from dataclasses import asdict, dataclass


PREFERRED_PLAN_ORDER = [
    "stage6_answer_packet.json",
    "stage8_real_site_approval_demo.json",
    "stage6_follow_answer_packet.json",
    "stage6_answer_packet_provider.json",
    "stage6_capture_packet.json",
    "stage3_local_task.json",
]


@dataclass(frozen=True)
class LaunchPlanOption:
    name: str
    summary: str
    requires_input_url: bool = False
    requires_follow_target_url: bool = False
    requires_proposal_target_url: bool = False
    uses_fixed_urls: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


PLAN_METADATA: dict[str, LaunchPlanOption] = {
    "stage3_local_task.json": LaunchPlanOption(
        name="stage3_local_task.json",
        summary="Local-only smoke script. Ignores website fields.",
        uses_fixed_urls=True,
    ),
    "stage5_demo_fetch.json": LaunchPlanOption(
        name="stage5_demo_fetch.json",
        summary="Fixed fetch demo against the internal test fixture. Ignores Input URL.",
        uses_fixed_urls=True,
    ),
    "stage5_fixture_fetch.json": LaunchPlanOption(
        name="stage5_fixture_fetch.json",
        summary="Fixture-backed fetch demo. Ignores Input URL.",
        uses_fixed_urls=True,
    ),
    "stage6_answer_packet.json": LaunchPlanOption(
        name="stage6_answer_packet.json",
        summary="Best default for research: read one page and answer your task from that page.",
        requires_input_url=True,
    ),
    "stage6_answer_packet_provider.json": LaunchPlanOption(
        name="stage6_answer_packet_provider.json",
        summary="Provider-mode version of the single-page answer packet.",
        requires_input_url=True,
    ),
    "stage6_browser_demo.json": LaunchPlanOption(
        name="stage6_browser_demo.json",
        summary="Fixed browser-render demo against the internal test fixture. Ignores Input URL.",
        uses_fixed_urls=True,
    ),
    "stage6_capture_packet.json": LaunchPlanOption(
        name="stage6_capture_packet.json",
        summary="Read one page and save a capture packet plus screenshot.",
        requires_input_url=True,
    ),
    "stage6_follow_answer_packet.json": LaunchPlanOption(
        name="stage6_follow_answer_packet.json",
        summary="Read one page, follow one target URL from it, then answer your task.",
        requires_input_url=True,
        requires_follow_target_url=True,
    ),
    "stage6b_browser_follow_demo.json": LaunchPlanOption(
        name="stage6b_browser_follow_demo.json",
        summary="Fixed follow-link browser demo against the internal test fixture. Ignores your URL fields.",
        uses_fixed_urls=True,
    ),
    "stage8_real_site_approval_demo.json": LaunchPlanOption(
        name="stage8_real_site_approval_demo.json",
        summary="Read one real page, write a brief, then request approval to POST a summary.",
        requires_input_url=True,
        requires_proposal_target_url=True,
    ),
}


def build_launch_plan_options(plan_names: list[str]) -> list[LaunchPlanOption]:
    known = []
    for name in plan_names:
        known.append(PLAN_METADATA.get(name, LaunchPlanOption(name=name, summary="Custom scripted plan.")))

    preferred_rank = {name: index for index, name in enumerate(PREFERRED_PLAN_ORDER)}
    return sorted(
        known,
        key=lambda option: (
            preferred_rank.get(option.name, len(PREFERRED_PLAN_ORDER)),
            option.name,
        ),
    )


def default_launch_plan_name(plan_names: list[str]) -> str:
    options = build_launch_plan_options(plan_names)
    if not options:
        return ""
    return options[0].name
