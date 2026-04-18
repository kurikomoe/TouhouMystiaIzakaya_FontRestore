from __future__ import annotations

import argparse
import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# Matches both CAB bundle format and sharedassets format
BUNDLE_MARKER_PATTERN = re.compile(r"-CAB-|-sharedassets\d+\.assets-")
PATH_ID_PATTERN = re.compile(r"(?:-CAB-[^-]+-|-sharedassets\d+\.assets-)(?P<path_id>-?\d+)$")
ATLAS_INDEX_PATTERN = re.compile(r" Atlas (\d+)$")


@dataclass(frozen=True)
class AssetInfo:
    path: Path
    export_name: str
    path_id: int
    role: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply one ref->input font mapping and output remapped JSONs for UABEA."
    )
    parser.add_argument("--input", required=True, dest="input_prefix", help="Input export prefix in --input-dir")
    parser.add_argument("--input-dir", default="@now", help="Directory with input exports")
    parser.add_argument("--ref", required=True, dest="ref_prefix", help="Reference export prefix in --ref-dir")
    parser.add_argument("--ref-dir", default="@orig", help="Directory with reference exports")
    parser.add_argument("--output-dir", required=True, help="Directory for generated exports")
    parser.add_argument(
        "--line-height-scale",
        type=float,
        default=1.0,
        help="Optional line-height multiplier, use 1.2 for SourceHanSans-Regular only",
    )
    return parser.parse_args()


def extract_export_name(path: Path) -> str:
    stem = path.stem
    m = BUNDLE_MARKER_PATTERN.search(stem)
    if not m:
        raise ValueError(f"Unsupported export filename: {path.name}")
    return stem[: m.start()]


def extract_path_id(path: Path) -> int:
    match = PATH_ID_PATTERN.search(path.stem)
    if not match:
        raise ValueError(f"Cannot extract PathID from {path.name}")
    return int(match.group("path_id"))


def split_suffix(export_name: str, prefix: str) -> str | None:
    if export_name == prefix:
        return ""
    if export_name.startswith(prefix + " "):
        return export_name[len(prefix) :]
    return None


def classify_role_from_suffix(suffix: str, is_input: bool) -> str:
    if suffix == " Atlas Material":
        return "atlas_material"
    if suffix in {" Outlined", " SDF-outlined"}:
        return "outlined_material"
    if suffix == " SDF-Bold":
        return "bold_material"

    atlas_index = ATLAS_INDEX_PATTERN.search(suffix)
    if atlas_index:
        return f"atlas_{atlas_index.group(1)}"

    if suffix == " Atlas":
        return "atlas_base"
    if suffix == " SDF":
        return "sdf_font"
    if suffix == "":
        return "source_font" if is_input else "sdf_font"

    raise ValueError(f"Unsupported asset suffix: {suffix!r}")


def collect_assets(directory: Path, prefix: str, is_input: bool) -> tuple[dict[str, AssetInfo], list[str]]:
    assets: dict[str, AssetInfo] = {}
    skipped: list[str] = []
    for path in sorted(directory.glob("*.json")):
        export_name = extract_export_name(path)
        suffix = split_suffix(export_name, prefix)
        if suffix is None:
            continue

        try:
            role = classify_role_from_suffix(suffix, is_input=is_input)
        except ValueError:
            skipped.append(path.name)
            continue
        info = AssetInfo(
            path=path,
            export_name=export_name,
            path_id=extract_path_id(path),
            role=role,
        )
        if role in assets:
            raise ValueError(f"Duplicate role {role!r} for prefix {prefix!r}: {path.name}")
        assets[role] = info

    return assets, skipped


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def validate_output_directory(input_dir: Path, ref_dir: Path, output_dir: Path) -> None:
    protected_dirs = [input_dir, ref_dir]
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
    if scale == 1.0:
        return

    face_info = asset.get("m_FaceInfo")
    if not isinstance(face_info, dict):
        return

    point_size = face_info.get("m_PointSize")
    if isinstance(point_size, (int, float)):
        face_info["m_LineHeight"] = round(float(point_size) * scale, 6)


def apply_line_height_scale_to_legacy_font(asset: dict[str, Any], scale: float) -> None:
    if scale == 1.0:
        return

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


def build_path_id_map(ref_assets: dict[str, AssetInfo], input_assets: dict[str, AssetInfo], ref_font: dict[str, Any], input_font: dict[str, Any]) -> dict[int, int]:
    path_id_map: dict[int, int] = {}
    for role in (
        "sdf_font",
        "atlas_material",
        "outlined_material",
        "atlas_base",
        "atlas_1",
        "atlas_2",
        "atlas_3",
        "atlas_4",
    ):
        if role in ref_assets and role in input_assets:
            path_id_map[ref_assets[role].path_id] = input_assets[role].path_id

    old_source_path_id = ref_font["m_SourceFontFile"]["m_PathID"]
    new_source_path_id = input_font["m_SourceFontFile"]["m_PathID"]
    path_id_map[old_source_path_id] = new_source_path_id
    return path_id_map


def generate_outputs(input_assets: dict[str, AssetInfo], ref_assets: dict[str, AssetInfo], output_dir: Path, line_height_scale: float) -> list[Path]:
    ensure_required_roles(input_assets, ["sdf_font", "atlas_material", "atlas_base"], label="input exports")
    ensure_required_roles(ref_assets, ["sdf_font", "atlas_material", "atlas_base"], label="reference exports")

    output_dir.mkdir(parents=True, exist_ok=True)

    input_font = load_json(input_assets["sdf_font"].path)
    ref_font = load_json(ref_assets["sdf_font"].path)
    path_id_map = build_path_id_map(ref_assets, input_assets, ref_font, input_font)

    source_role_for_input_role = {
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
    for role, input_asset in sorted(input_assets.items(), key=lambda item: item[1].path.name):
        template = load_json(input_asset.path)
        source_role = source_role_for_input_role.get(role)

        if source_role is None:
            result = copy.deepcopy(template)
            if role == "source_font":
                apply_line_height_scale_to_legacy_font(result, line_height_scale)
        else:
            if source_role not in ref_assets:
                continue
            source = load_json(ref_assets[source_role].path)
            if role == "sdf_font":
                result = build_font_asset(template, source, path_id_map)
                apply_line_height_scale_to_tmp_font(result, line_height_scale)
            elif role in {"atlas_material", "bold_material", "outlined_material"}:
                result = build_material(template, source, path_id_map)
            else:
                result = build_texture_asset(template, source)

        output_path = output_dir / input_asset.path.name
        write_json(output_path, result)
        written_files.append(output_path)

    return written_files


def main() -> None:
    args = parse_args()
    base_dir = Path.cwd()

    input_dir = (base_dir / args.input_dir).resolve()
    ref_dir = (base_dir / args.ref_dir).resolve()
    output_dir = (base_dir / args.output_dir).resolve()

    validate_output_directory(input_dir=input_dir, ref_dir=ref_dir, output_dir=output_dir)

    input_assets, input_skipped = collect_assets(input_dir, prefix=args.input_prefix, is_input=True)
    ref_assets, ref_skipped = collect_assets(ref_dir, prefix=args.ref_prefix, is_input=False)

    if not input_assets:
        raise ValueError(f"No input assets found for prefix: {args.input_prefix}")
    if not ref_assets:
        raise ValueError(f"No reference assets found for prefix: {args.ref_prefix}")

    written_files = generate_outputs(
        input_assets=input_assets,
        ref_assets=ref_assets,
        output_dir=output_dir,
        line_height_scale=args.line_height_scale,
    )

    print(
        f"Generated {len(written_files)} files in {output_dir} "
        f"for mapping {args.input_prefix} <- {args.ref_prefix}"
    )
    if input_skipped or ref_skipped:
        print("Skipped unsupported assets:")
        for name in input_skipped:
            print(f"  [input] {name}")
        for name in ref_skipped:
            print(f"  [ref] {name}")
    for path in written_files:
        print(path.name)


if __name__ == "__main__":
    main()
