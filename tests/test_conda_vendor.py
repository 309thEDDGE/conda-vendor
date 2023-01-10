from unittest.mock import patch

from conda_vendor.conda_vendor import _get_conda_platform


@patch("sys.platform", "linux")
@patch("struct.calcsize")
def test_get_conda_platform_32bit(mock_struct) -> None:
    mock_struct.return_value = 4
    expected = "linux-32"
    result = _get_conda_platform()
    assert expected == result
    assert mock_struct.call_count == 1


@patch("sys.platform", "darwin")
@patch("struct.calcsize")
def test_get_conda_platform_64bi(mock_struct) -> None:
    mock_struct.return_value = 8
    expected = "osx-64"
    result = _get_conda_platform()
    assert expected == result
    assert mock_struct.call_count == 1


def test_get_conda_platform_passthrough():
    test_platforms = ["linux-64", "linux-32", "win-64", "win-32", "osx-64"]
    expected_returns = ["linux-64", "linux-32", "win-64", "win-32", "osx-64"]
    actual_returns = [_get_conda_platform(p) for p in test_platforms]
    assert set(actual_returns) == set(expected_returns)
