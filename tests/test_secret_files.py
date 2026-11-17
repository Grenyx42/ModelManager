import stat

from modelmanager.secret_files import write_secret_file


def test_write_then_read_round_trips(tmp_path):
    path = tmp_path / "secret"

    write_secret_file(path, "hello\n")

    assert path.read_text() == "hello\n"


def test_written_file_is_only_readable_by_owner(tmp_path):
    path = tmp_path / "secret"

    write_secret_file(path, "hello\n")

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_creates_parent_directories(tmp_path):
    path = tmp_path / "nested" / "dir" / "secret"

    write_secret_file(path, "hello\n")

    assert path.read_text() == "hello\n"


def test_overwrites_existing_file_content(tmp_path):
    path = tmp_path / "secret"
    write_secret_file(path, "old\n")

    write_secret_file(path, "new\n")

    assert path.read_text() == "new\n"


def test_no_leftover_temp_file_after_write(tmp_path):
    path = tmp_path / "secret"

    write_secret_file(path, "hello\n")

    assert list(tmp_path.iterdir()) == [path]


def test_refuses_to_follow_a_preexisting_symlink_at_the_destination(tmp_path):
    real_target = tmp_path / "elsewhere"
    link_path = tmp_path / "secret"
    link_path.symlink_to(real_target)

    write_secret_file(link_path, "hello\n")

    # The symlink itself got replaced by a real file, not followed -
    # `elsewhere` was never touched.
    assert not real_target.exists()
    assert link_path.is_symlink() is False
    assert link_path.read_text() == "hello\n"
