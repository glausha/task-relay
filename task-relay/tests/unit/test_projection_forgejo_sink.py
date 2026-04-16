from __future__ import annotations

from task_relay.projection.forgejo_sink import ForgejoSink
from task_relay.projection.labels import MANAGED_LABELS


def test_diff_labels_preserves_unmanaged_labels_and_applies_desired() -> None:
    sink = ForgejoSink(base_url="http://forgejo.local", token="token", owner="org", repo="repo")

    final_names = sink._diff_labels(
        current=[{"name": "critical"}, {"name": "bug"}, {"name": "ux"}],
        managed=MANAGED_LABELS,
        desired=["cancelled"],
    )

    assert final_names == ["bug", "cancelled", "ux"]


def test_diff_labels_keeps_unmanaged_labels_when_desired_is_empty() -> None:
    sink = ForgejoSink(base_url="http://forgejo.local", token="token", owner="org", repo="repo")

    final_names = sink._diff_labels(
        current=[{"name": "critical"}, {"name": "bug"}, {"name": "ux"}],
        managed=MANAGED_LABELS,
        desired=[],
    )

    assert final_names == ["bug", "ux"]


def test_diff_labels_deduplicates_managed_labels_from_current_and_desired() -> None:
    sink = ForgejoSink(base_url="http://forgejo.local", token="token", owner="org", repo="repo")

    final_names = sink._diff_labels(
        current=[{"name": "critical"}, {"name": "critical"}, {"name": "bug"}],
        managed=MANAGED_LABELS,
        desired=["critical", "critical"],
    )

    assert final_names == ["bug", "critical"]
