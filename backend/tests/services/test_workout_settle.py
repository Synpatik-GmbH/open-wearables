from app.services.apple.healthkit.workout_settle import should_emit

T, CAP = 0.95, 5.0


def test_no_hr_emits_immediately():
    # completeness None → nothing to wait for
    assert should_emit(None, None, 0.0, T, CAP) is True


def test_first_tick_growing_waits():
    # prev None, has HR, under target, under cap → wait
    assert should_emit(None, 0.41, 1.0, T, CAP) is False


def test_stable_emits():
    # completeness unchanged tick-over-tick (and > 0) → settled → emit
    assert should_emit(1.0, 1.0, 3.0, T, CAP) is True


def test_grew_waits():
    # grew tick-over-tick, still under target/cap → keep waiting.
    # NOTE: brief listed completeness=1.0 here, but 1.0 >= target(0.95) hits the
    # target fast-path (a 100%-complete trace must emit, not wait). Use 0.80 to
    # actually exercise the "grew, still under target" wait path the case intends.
    assert should_emit(0.41, 0.80, 2.0, T, CAP) is False


def test_target_fast_path():
    # >= target → emit now
    assert should_emit(None, 0.96, 1.0, T, CAP) is True


def test_hard_cap_emits():
    # elapsed >= cap → emit whatever
    assert should_emit(0.41, 0.80, 6.0, T, CAP) is True


def test_stable_at_zero_does_not_emit():
    # stable at 0 must NOT early-emit (the > 0 guard); a real HR-less
    # workout hits the completeness-is-None branch instead
    assert should_emit(0.0, 0.0, 2.0, T, CAP) is False
