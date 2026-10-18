from pathlib import Path

from offline_doc_tools.libreoffice import ENV_VAR, find_soffice, missing_libreoffice_message


def _exists(*paths: str):
    existing = {str(Path(p)) for p in paths}
    return lambda path: str(Path(path)) in existing


def _no_which(_name: str) -> None:
    return None


def test_env_var_override_wins(tmp_path):
    custom = tmp_path / "custom" / "soffice.exe"
    found = find_soffice(
        env={ENV_VAR: str(custom)},
        which=lambda name: r"C:\on-path\soffice.exe",
        platform="win32",
        is_file=_exists(str(custom), r"C:\on-path\soffice.exe"),
    )
    assert found == custom


def test_env_var_pointing_to_missing_file_is_not_silently_replaced():
    found = find_soffice(
        env={ENV_VAR: r"D:\nowhere\soffice.exe"},
        which=lambda name: r"C:\on-path\soffice.exe",
        platform="win32",
        is_file=_exists(r"C:\on-path\soffice.exe"),
    )
    assert found is None


def test_found_on_path():
    found = find_soffice(
        env={},
        which=lambda name: "/usr/bin/soffice" if name == "soffice" else None,
        platform="linux",
        is_file=_exists("/usr/bin/soffice"),
    )
    assert found == Path("/usr/bin/soffice")


def test_windows_program_files_location():
    expected = r"C:\Program Files\LibreOffice\program\soffice.exe"
    found = find_soffice(
        env={"ProgramFiles": r"C:\Program Files"},
        which=_no_which,
        platform="win32",
        is_file=_exists(expected),
    )
    assert found == Path(expected)


def test_windows_program_files_x86_location():
    expected = r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"
    found = find_soffice(
        env={"ProgramFiles": r"C:\Program Files", "ProgramFiles(x86)": r"C:\Program Files (x86)"},
        which=_no_which,
        platform="win32",
        is_file=_exists(expected),
    )
    assert found == Path(expected)


def test_windows_prefers_standard_location_over_path():
    standard = r"C:\Program Files\LibreOffice\program\soffice.exe"
    found = find_soffice(
        env={"ProgramFiles": r"C:\Program Files"},
        which=lambda name: r"C:\elsewhere\soffice.exe",
        platform="win32",
        is_file=_exists(standard, r"C:\elsewhere\soffice.exe"),
    )
    assert found == Path(standard)


def test_windows_path_executable_used_when_not_installed_in_standard_location():
    found = find_soffice(
        env={},
        which=lambda name: r"D:\Apps\LibreOffice\program\soffice.exe",
        platform="win32",
        is_file=_exists(r"D:\Apps\LibreOffice\program\soffice.exe"),
        cwd=Path(r"C:\Users\someone"),
    )
    assert found == Path(r"D:\Apps\LibreOffice\program\soffice.exe")


def test_windows_rejects_script_found_on_path():
    found = find_soffice(
        env={},
        which=lambda name: r"D:\tools\soffice.bat",
        platform="win32",
        is_file=_exists(r"D:\tools\soffice.bat"),
        cwd=Path(r"C:\Users\someone"),
    )
    assert found is None


def test_windows_rejects_executable_in_current_directory():
    found = find_soffice(
        env={},
        which=lambda name: r"C:\Downloads\soffice.exe",
        platform="win32",
        is_file=_exists(r"C:\Downloads\soffice.exe"),
        cwd=Path(r"C:\Downloads"),
    )
    assert found is None


def test_macos_application_bundle():
    expected = "/Applications/LibreOffice.app/Contents/MacOS/soffice"
    found = find_soffice(env={}, which=_no_which, platform="darwin", is_file=_exists(expected))
    assert found == Path(expected)


def test_not_found_anywhere():
    found = find_soffice(
        env={"ProgramFiles": r"C:\Program Files"},
        which=_no_which,
        platform="win32",
        is_file=lambda path: False,
    )
    assert found is None


def test_missing_message_explains_install_and_override():
    message = missing_libreoffice_message(env={})
    assert "LibreOffice" in message
    assert "libreoffice.org" in message
    assert ENV_VAR in message


def test_missing_message_mentions_bad_override():
    message = missing_libreoffice_message(env={ENV_VAR: r"D:\nowhere\soffice.exe"})
    assert r"D:\nowhere\soffice.exe" in message
