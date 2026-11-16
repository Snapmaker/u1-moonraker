import pathlib
import sys

import pytest

sys.path.insert(
    0, str(pathlib.Path(__file__).parents[2] / "moonraker/components/file_manager")
)
import metadata  # noqa: E402


@pytest.mark.parametrize("key", metadata.SUPPORTED_DATA)
def test_every_supported_field_has_a_base_parser(key):
    # extract_metadata() calls parse_<key> on whichever slicer was detected
    assert hasattr(metadata.BaseSlicer, "parse_" + key)


@pytest.mark.parametrize("header, slicer", [
    (";FLAVOR:Marlin\n;Generated with Cura_SteamEngine 5.8.0\n", "Cura"),
    ("; hand written start\n", "Unknown"),
])
def test_extract_metadata_outside_the_orca_family(tmp_path, header, slicer):
    gcode = tmp_path / "t.gcode"
    gcode.write_text(header + "G28\nG1 X10 Y10 E1 F600\n")
    result = metadata.extract_metadata(str(gcode), False)
    assert result["slicer"] == slicer
    assert result["size"] == gcode.stat().st_size
