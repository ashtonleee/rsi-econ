from seedlib import render_seed_status


def test_render_seed_status():
    assert render_seed_status("stage3") == "seed agent ready: stage3"
