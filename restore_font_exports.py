from __future__ import annotations

import argparse
import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


EXPORT_NAME_MARKER = "-CAB-"
PATH_ID_PATTERN = re.compile(r"-CAB-[^-]+-(?P<path_id>-?\d+)$")
ATLAS_INDEX_PATTERN = re.compile(r" Atlas (\d+)$")


@dataclass(frozen=True)
class AssetInfo:
    path: Path
    export_name: str
    path_id: int
    role: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Merge old Unity font exports into the new export mapping for UABEA."
    )
    parser.add_argument("--new-dir", default="@new", help="Directory with the new exports")
    parser.add_argument("--orig-dir", default="@orig", help="Directory with the original exports")
    parser.add_argument("--output-dir", default="@output", help="Directory for generated exports")
    parser.add_argument(
        "--line-height-scale",
        type=float,
        default=1.2,
        help="Line-height multiplier applied to generated font assets (default: 1.2)",
    )
    return parser.parse_args()


def extract_export_name(path: Path) -> str:
    stem = path.stem
    if EXPORT_NAME_MARKER not in stem:
        raise ValueError(f"Unsupported export filename: {path.name}")
    return stem.split(EXPORT_NAME_MARKER, 1)[0]


def extract_path_id(path: Path) -> int:
    match = PATH_ID_PATTERN.search(path.stem)
    if not match:
        raise ValueError(f"Cannot extract PathID from {path.name}")
    return int(match.group("path_id"))


def classify_role(export_name: str, is_new: bool) -> str:
    if " Atlas Material" in export_name:
        return "atlas_material"
    if " SDF-outlined" in export_name or " Outlined" in export_name:
        return "outlined_material"
    if " SDF-Bold" in export_name:
        return "bold_material"

    atlas_index = ATLAS_INDEX_PATTERN.search(export_name)
    if atlas_index:
        return f"atlas_{atlas_index.group(1)}"
    if export_name.endswith(" Atlas"):
        return "atlas_base"
    if " SDF" in export_name:
        return "sdf_font"
    if is_new:
        return "source_font"
    return "sdf_font"


def collect_assets(directory: Path, is_new: bool) -> dict[str, AssetInfo]:
    assets: dict[str, AssetInfo] = {}
    for path in sorted(directory.glob("*.json")):
        export_name = extract_export_name(path)
        role = classify_role(export_name, is_new=is_new)
        info = AssetInfo(
            path=path,
            export_name=export_name,
            path_id=extract_path_id(path),
            role=role,
        )
        if role in assets:
            raise ValueError(f"Duplicate role {role!r} in {directory}: {path.name}")
        assets[role] = info
    return assets


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def validate_output_directory(new_dir: Path, orig_dir: Path, output_dir: Path) -> None:
    # Safety guard: never allow writing into @new or @orig (or their subfolders).
    protected_dirs = [new_dir, orig_dir]
    for protected_dir in protected_dirs:
        if output_dir == protected_dir or output_dir.is_relative_to(protected_dir):
            raise ValueError(
                f"Refuse to write outputs into protected directory: {protected_dir}"
            )


def rewrite_path_ids(node: Any, path_id_map: dict[int, int]) -> Any:
    if isinstance(node, dict):
        if (
            "m_FileID" in node
            and "m_PathID" in node
            and isinstance(node["m_FileID"], int)
            and isinstance(node["m_PathID"], int)
            and node["m_FileID"] == 0
            and node["m_PathID"] in path_id_map
        ):
            node["m_PathID"] = path_id_map[node["m_PathID"]]
        for value in node.values():
            rewrite_path_ids(value, path_id_map)
    elif isinstance(node, list):
        for item in node:
            rewrite_path_ids(item, path_id_map)
    return node


def merge_named_property_array(template_items: list[dict[str, Any]], source_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    source_by_name = {item["first"]: copy.deepcopy(item) for item in source_items}
    merged: list[dict[str, Any]] = []
    for item in template_items:
        key = item["first"]
        merged.append(source_by_name.get(key, copy.deepcopy(item)))
    return merged


def build_material(template: dict[str, Any], source: dict[str, Any], path_id_map: dict[int, int]) -> dict[str, Any]:
    result = copy.deepcopy(template)
    source_props = source["m_SavedProperties"]
    result_props = result["m_SavedProperties"]

    for property_name in ("m_TexEnvs", "m_Ints", "m_Floats", "m_Colors"):
        result_props[property_name]["Array"] = merge_named_property_array(
            result_props[property_name]["Array"],
            source_props[property_name]["Array"],
        )

    rewrite_path_ids(result, path_id_map)
    result["m_Name"] = template["m_Name"]
    return result


def build_font_asset(template: dict[str, Any], source: dict[str, Any], path_id_map: dict[int, int]) -> dict[str, Any]:
    result = copy.deepcopy(source)

    for field_name in (
        "m_GameObject",
        "m_Enabled",
        "m_Script",
        "m_Name",
        "hashCode",
        "materialHashCode",
        "m_Version",
        "m_SourceFontFileGUID",
    ):
        result[field_name] = copy.deepcopy(template[field_name])

    rewrite_path_ids(result, path_id_map)

    result["material"] = copy.deepcopy(template["material"])
    result["m_SourceFontFile"] = copy.deepcopy(template["m_SourceFontFile"])

    if "m_CreationSettings" in result and "m_CreationSettings" in template:
        result["m_CreationSettings"]["sourceFontFileGUID"] = template["m_CreationSettings"]["sourceFontFileGUID"]
        result["m_CreationSettings"]["sourceFontFileName"] = template["m_CreationSettings"]["sourceFontFileName"]

    return result


def apply_line_height_scale_to_tmp_font(asset: dict[str, Any], scale: float) -> None:
    face_info = asset.get("m_FaceInfo")
    if not isinstance(face_info, dict):
        return

    point_size = face_info.get("m_PointSize")
    if isinstance(point_size, (int, float)):
        face_info["m_LineHeight"] = round(float(point_size) * scale, 6)


def apply_line_height_scale_to_legacy_font(asset: dict[str, Any], scale: float) -> None:
    font_size = asset.get("m_FontSize")
    if isinstance(font_size, (int, float)):
        asset["m_LineSpacing"] = round(float(font_size) * scale, 6)


def build_texture_asset(template: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(source)
    if "m_Name" in template and "m_Name" in result:
        result["m_Name"] = template["m_Name"]
    return result


def ensure_required_roles(assets: dict[str, AssetInfo], roles: list[str], label: str) -> None:
    missing = [role for role in roles if role not in assets]
    if missing:
        raise ValueError(f"Missing roles in {label}: {', '.join(missing)}")


def build_path_id_map(orig_assets: dict[str, AssetInfo], new_assets: dict[str, AssetInfo], orig_font: dict[str, Any], new_font: dict[str, Any]) -> dict[int, int]:
    role_pairs = [
        ("sdf_font", "sdf_font"),
        ("atlas_material", "atlas_material"),
        ("outlined_material", "outlined_material"),
        ("atlas_base", "atlas_base"),
        ("atlas_1", "atlas_1"),
        ("atlas_2", "atlas_2"),
        ("atlas_3", "atlas_3"),
        ("atlas_4", "atlas_4"),
    ]
    path_id_map: dict[int, int] = {}
    for orig_role, new_role in role_pairs:
        if orig_role in orig_assets and new_role in new_assets:
            path_id_map[orig_assets[orig_role].path_id] = new_assets[new_role].path_id

    old_source_path_id = orig_font["m_SourceFontFile"]["m_PathID"]
    new_source_path_id = new_font["m_SourceFontFile"]["m_PathID"]
    path_id_map[old_source_path_id] = new_source_path_id
    return path_id_map


def generate_outputs(new_dir: Path, orig_dir: Path, output_dir: Path, line_height_scale: float) -> list[Path]:
    new_assets = collect_assets(new_dir, is_new=True)
    orig_assets = collect_assets(orig_dir, is_new=False)

    ensure_required_roles(
        new_assets,
        [
            "sdf_font",
            "atlas_material",
            "outlined_material",
            "bold_material",
            "atlas_base",
            "atlas_1",
            "atlas_2",
            "atlas_3",
            "atlas_4",
        ],
        label="new exports",
    )
    ensure_required_roles(
        orig_assets,
        [
            "sdf_font",
            "atlas_material",
            "outlined_material",
            "atlas_base",
            "atlas_1",
            "atlas_2",
            "atlas_3",
            "atlas_4",
        ],
        label="original exports",
    )

    output_dir.mkdir(parents=True, exist_ok=True)

    new_font = load_json(new_assets["sdf_font"].path)
    orig_font = load_json(orig_assets["sdf_font"].path)
    path_id_map = build_path_id_map(orig_assets, new_assets, orig_font, new_font)

    source_role_for_new_role = {
        "source_font": None,
        "sdf_font": "sdf_font",
        "atlas_material": "atlas_material",
        "bold_material": "atlas_material",
        "outlined_material": "outlined_material",
        "atlas_base": "atlas_base",
        "atlas_1": "atlas_1",
        "atlas_2": "atlas_2",
        "atlas_3": "atlas_3",
        "atlas_4": "atlas_4",
    }

    written_files: list[Path] = []
    for role, new_asset in sorted(new_assets.items(), key=lambda item: item[1].path.name):
        template = load_json(new_asset.path)
        source_role = source_role_for_new_role.get(role)

        if source_role is None:
            result = copy.deepcopy(template)
            if role == "source_font":
                apply_line_height_scale_to_legacy_font(result, line_height_scale)
        else:
            source = load_json(orig_assets[source_role].path)
            if role == "sdf_font":
                result = build_font_asset(template, source, path_id_map)
                apply_line_height_scale_to_tmp_font(result, line_height_scale)
            elif role in {"atlas_material", "bold_material", "outlined_material"}:
                result = build_material(template, source, path_id_map)
            else:
                result = build_texture_asset(template, source)

        output_path = output_dir / new_asset.path.name
        write_json(output_path, result)
        written_files.append(output_path)

    return written_files


def main() -> None:
    args = parse_args()
    base_dir = Path.cwd()
    new_dir = (base_dir / args.new_dir).resolve()
    orig_dir = (base_dir / args.orig_dir).resolve()
    output_dir = (base_dir / args.output_dir).resolve()

    validate_output_directory(new_dir=new_dir, orig_dir=orig_dir, output_dir=output_dir)

    written_files = generate_outputs(
        new_dir=new_dir,
        orig_dir=orig_dir,
        output_dir=output_dir,
        line_height_scale=args.line_height_scale,
    )
    print(f"Generated {len(written_files)} files in {output_dir}")
    for path in written_files:
        print(path.name)


if __name__ == "__main__":
    main()
