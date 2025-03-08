from pacupdate.main import get_log_diff_warnings


def test_get_log_diff_warnings():
    old_log = [
        "[2022-06-06T21:24:21+0200] [ALPM] warning: warning 1",
        "[2022-06-06T21:24:21+0200] [ALPM] upgraded something",
        "[2022-06-06T21:24:21+0200] [ALPM] upgraded something",
        "[2022-06-06T21:24:21+0200] [ALPM] upgraded something",
        "[2022-06-06T21:24:21+0200] [ALPM] warning: warning 2",
    ]
    new_log = [
        "[2022-06-06T21:24:21+0200] [ALPM] warning: warning 1",
        "[2022-06-06T21:24:21+0200] [ALPM] upgraded something",
        "[2022-06-06T21:24:21+0200] [ALPM] upgraded something",
        "[2022-06-06T21:24:21+0200] [ALPM] upgraded something",
        "[2022-06-06T21:24:21+0200] [ALPM] warning: warning 2",
        "[2022-06-06T21:24:21+0200] [ALPM] warning: warning 3",
        "[2022-06-06T21:24:21+0200] [ALPM] upgraded something",
        "[2022-06-06T21:24:21+0200] [ALPM] upgraded something",
        "[2022-06-06T21:24:21+0200] [ALPM] warning: warning 4",
        "[2022-06-06T21:24:21+0200] [ALPM] upgraded something",
        "[2022-06-06T21:24:21+0200] [ALPM] warning: warning 5",
    ]
    assert get_log_diff_warnings(old_log, new_log) == [
        "warning 3",
        "warning 4",
        "warning 5",
    ]
