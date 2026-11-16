import pathlib
import sys

import pytest

sys.path.insert(
    0, str(pathlib.Path(__file__).parents[2] / "moonraker/components/file_manager")
)
import metadata  # noqa: E402


@pytest.mark.parametrize("footer_value, expected", [
    ("PLA", "PLA"),
    ("PLA;PETG;TPU;PLA", "PLA;PETG;TPU;PLA"),
    # OrcaSlicer quotes only the entries that contain a space
    ('"PLA Matte"', "PLA Matte"),
    ('"PLA Matte";PLA;PLA;PLA', "PLA Matte;PLA;PLA;PLA"),
    ('PLA;"PLA Matte";PETG;"PLA Silk"', "PLA;PLA Matte;PETG;PLA Silk"),
    ('PLA;"PLA Matte"', "PLA;PLA Matte"),
])
def test_prusa_family_filament_type(footer_value, expected):
    slicer = metadata.PrusaSlicer("unused.gcode")
    slicer.footer_data = (
        "; filament_soluble = 0\n"
        f"; filament_type = {footer_value}\n"
        "; filament_vendor = Generic\n"
    )
    assert slicer.parse_filament_type() == expected


def test_prusa_family_filament_type_missing():
    slicer = metadata.PrusaSlicer("unused.gcode")
    slicer.footer_data = "; filament_soluble = 0\n"
    assert slicer.parse_filament_type() is None
