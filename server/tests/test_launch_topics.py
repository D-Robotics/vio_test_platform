"""Regression tests for the stereo topic convention (topic_camera_stereo).

The rig's combined stereo topic is ALWAYS the RAW name
(``/sub_image_combine_raw``). The VIO derives the CompressedImage sub-topic by
rewriting the trailing token (raw -> jpeg), so a non-canonical image topic
produces a *_jjpeg mismatch and a FastCDR deserialization crash (exit -6).

These tests run fully offline (no board, no bag file): ``_launch_parts`` is a
pure function, and ``pick_default_topics`` is tested against a faked bag_info.
"""
import io
from contextlib import redirect_stderr

from server import backtest, config, datasets


def _parts(**overrides):
    kw = dict(
        image_topic="/sub_image_combine_raw",
        compressed=False,
        config_path="/cfg/estimator_config.yaml",
        run_dir="/runs/x",
        mnt_ds="/mnt/vio_datasets/ds",
        offline_bag=True,
        verbosity="INFO",
        vio_log_level="warn",
        extra_args="",
    )
    kw.update(overrides)
    return backtest._launch_parts(**kw)


def test_compressed_bag_sets_stereo_raw_and_subflag():
    parts = _parts(image_topic=config.CANONICAL_STEREO_TOPIC, compressed=True)
    assert "topic_camera_stereo:=/sub_image_combine_raw" in parts
    assert "sub_from_compressed_image:=True" in parts
    # never a non-canonical topic
    assert not any("jpeg" in p for p in parts)


def test_raw_bag_sets_stereo_raw_without_subflag():
    parts = _parts(image_topic=config.CANONICAL_STEREO_TOPIC, compressed=False)
    assert "topic_camera_stereo:=/sub_image_combine_raw" in parts
    assert "sub_from_compressed_image:=True" not in parts
    assert "sub_from_compressed_image:=False" not in parts


def test_default_image_topic_resolves_to_canonical():
    parts = _parts(image_topic=None)
    assert "topic_camera_stereo:=/sub_image_combine_raw" in parts


def test_noncanonical_topic_warns():
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        parts = _parts(image_topic="/sub_image_combine_jpeg")
    assert "topic_camera_stereo:=/sub_image_combine_jpeg" in parts
    assert "non-canonical topic_camera_stereo" in stderr.getvalue()


def test_pick_default_topics_always_returns_canonical_even_for_jpeg_bag(monkeypatch):
    faked = {
        "topics": [
            {"name": "/sub_image_combine_jpeg", "type": "sensor_msgs/msg/CompressedImage"},
            {"name": "/drobotics_imu/bmi08x_imu", "type": "sensor_msgs/msg/Imu"},
        ]
    }
    monkeypatch.setattr(datasets, "bag_info", lambda name: faked)
    picked = datasets.pick_default_topics("whatever")
    # launch topic = canonical RAW even though the bag only has jpeg
    assert picked["image"] == "/sub_image_combine_raw"
    assert picked["image_compressed"] is True
    assert picked["imu"] == "/drobotics_imu/bmi08x_imu"


def test_pick_frame_topic_returns_actual_bag_topic(monkeypatch):
    faked = {
        "topics": [
            {"name": "/sub_image_combine_jpeg", "type": "sensor_msgs/msg/CompressedImage"},
            {"name": "/drobotics_imu/bmi08x_imu", "type": "sensor_msgs/msg/Imu"},
        ]
    }
    monkeypatch.setattr(datasets, "bag_info", lambda name: faked)
    # frame/topic read must use the topic actually in the bag, not the RAW alias
    assert datasets.pick_frame_topic("whatever") == "/sub_image_combine_jpeg"


def test_pick_frame_topic_none_on_no_image(monkeypatch):
    faked = {"topics": [{"name": "/drobotics_imu/bmi08x_imu", "type": "sensor_msgs/msg/Imu"}]}
    monkeypatch.setattr(datasets, "bag_info", lambda name: faked)
    assert datasets.pick_frame_topic("whatever") is None


def test_default_launch_template_is_dataset_free_and_renderable():
    """The template must not hardcode any concrete dataset value, and every
    {{token}} must resolve under start_backtest's render context so a saved
    template works for any task."""
    import re
    text = backtest.default_launch_template()
    # pure placeholder form: no literal run_dir path, no concrete topic/compressed
    assert "{{run_dir}}" in text
    assert "{{config_path}}" in text
    assert "{{board_dataset}}" in text
    assert "{{dataset}}" in text
    assert "{{image_topic}}" in text
    assert "/sub_image_combine_jpeg" not in text
    assert "/sub_image_combine_raw" not in text  # topic goes through placeholder
    assert not re.search(r"results/runs/|/userdata/vio_backtest/runs/", text)

    ctx = {
        "run_dir": "/runs/x__baseline__ds",
        "config_path": "/cfg/estimator_config.yaml",
        "save_path": "/runs/x__baseline__ds/output",
        "dataset": "ysdata/some_dataset",
        "image_topic": "/sub_image_combine_raw",
        "board_dataset": "/mnt/vio_datasets/ysdata/some_dataset",
        "offline_bag": "true",
        "experiment": "",
        "board_base": "/userdata/vio_backtest",
        "runs_dir": "/userdata/vio_backtest/runs",
        "install_dir": "/userdata/vio_backtest/install",
        "current": "/userdata/vio_backtest/current",
        "_ros_env": "source /opt/tros/humble/setup.bash; ",
        "sub_from_compressed_image": "true",
        "imu_topic": "/drobotics_imu/bmi08x_imu",
        "verbosity": "INFO",
        "vio_log_level": "warn",
    }
    rendered = backtest._render_template(text, ctx)
    # no token left unfilled apart from the intentional script-header comment
    assert "{{run_dir}}" not in rendered
    assert "{{config_path}}" not in rendered
    assert "{{board_dataset}}" not in rendered
    assert "topic_camera_stereo:=/sub_image_combine_raw" in rendered
    assert "sub_from_compressed_image:=true" in rendered
    assert "config_path:=/cfg/estimator_config.yaml" in rendered
