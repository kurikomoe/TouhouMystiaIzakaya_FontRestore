build:
  # python ./restore_font_exports.py --input "JYHPHZ" --input-dir "@now/sharedassets" --ref "JYHPHZ" --ref-dir "@orig/sharedassets" --output-dir "@output/sharedassets"
  # python ./restore_font_exports.py --input "JYHPHZ" --input-dir "@now/systemfonts" --ref "JYHPHZ" --ref-dir "@orig/systemfonts" --output-dir "@output/systemfonts"
  # python ./restore_font_exports.py --input "SourceHanSans-Regular" --input-dir "@now/systemfonts" --ref "思源柔黑-Regular" --ref-dir "@orig/systemfonts" --output-dir "@output/systemfonts" --line-height-scale 1.2
  python ./restore_font_exports.py --input "SourceHanSans-Regular" --input-dir "@now/sharedassets" --ref "思源柔黑-Regular" --ref-dir "@orig/sharedassets" --output-dir "@output/sharedassets" --line-height-scale 1.2

release:
  mkdir -p @release
  cp -r docs README.md @release
