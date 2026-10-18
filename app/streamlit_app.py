"""Streamlit dashboard — Groww Review Pulse Agent (Phase 6).

UI only: no business logic lives here. Everything shown comes from
:mod:`groww_pulse.dashboard`, which reads the newest pulse that passes the Phase 5 publish
checks, plus its preprocessing funnel, analysis summary, and delivery ledger.

Delivery reuses the Phase 5 publisher unchanged:

* "Preview delivery (dry run)" makes no network call;
* the confirmed button stays disabled until every validator passed, ``DELIVERY_MODE=confirm``,
  delivery and the MCP token are configured, a dry run of this exact pulse was previewed in
  this session, and the confirmation box is ticked;
* it appends to the Google Doc and creates a Gmail **draft**. There is no send control, and
  ``--force`` is CLI-only;
* if a different version of the same ISO week was already delivered, the pulse is shown as
  blocked and cannot be delivered from here (the publisher refuses it too). When that other
  version reached only some targets, a separate section lets the operator finish *that*
  version, behind the same preview and confirmation gate.

Run locally:  streamlit run app/streamlit_app.py
"""

from __future__ import annotations

import json
import re
from typing import Any

import streamlit as st

from groww_pulse import __version__, dashboard
from groww_pulse.config import Settings, load_settings
from groww_pulse.delivery.contract import ALLOWED_TOOLS, FORBIDDEN_TOOLS
from groww_pulse.delivery.ledger import Target
from groww_pulse.errors import ConfigError, DeliveryError
from groww_pulse.logging_setup import bind_run_id, configure_logging, mask
from groww_pulse.report.render import quote_display

TARGET_LABELS = {
    Target.GOOGLE_DOC.value: "Google Doc append",
    Target.GMAIL_DRAFT.value: "Gmail draft",
}
PIPELINE_COMMANDS = """\
groww-pulse import data/imports/<export>.csv --as-of <YYYY-MM-DD>
groww-pulse preprocess --as-of <YYYY-MM-DD>
groww-pulse analyze
groww-pulse report"""
_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]<>()#+\-.!|~])")


def _md(text: str) -> str:
    """Escape Markdown so stored text is shown literally."""
    return _MD_SPECIAL.sub(r"\\\1", text)


def _masked_json(value: dict[str, Any]) -> str:
    return mask(json.dumps(value, indent=2, ensure_ascii=False))


def _readiness(settings: Settings) -> list[tuple[str, bool, str]]:
    checks = []
    for label, check in (
        ("LLM analysis", settings.require_llm),
        ("MCP access", settings.require_mcp),
        ("Delivery targets", settings.require_delivery),
    ):
        try:
            check()
            checks.append((label, True, "configured"))
        except ConfigError as exc:
            checks.append((label, False, str(exc)))
    return checks


# ---------------------------------------------------------------- sections
def _rejected(board: dashboard.Dashboard) -> None:
    if not board.rejected:
        return
    with st.expander(
        f"{len(board.rejected)} newer pulse file(s) failed verification and are not shown",
        expanded=board.pulse is None,
    ):
        for item in board.rejected:
            st.error(f"Run {item.run_id}: {item.reason}")


def _empty_state() -> None:
    st.info(
        "No validated weekly pulse yet. Import a permitted Google Play export and run the "
        "pipeline, then reload this page:"
    )
    st.code(PIPELINE_COMMANDS, language="bash")


def _pulse_tab(view: dashboard.DashboardPulse) -> None:
    pulse = view.pulse
    passed = sum(v["passed"] for v in view.validators)
    cols = st.columns(4)
    cols[0].metric("ISO week", pulse.iso_week)
    cols[1].metric("Reviews analyzed", pulse.n_analyzed, help=f"of {pulse.n_collected} collected")
    cols[2].metric("Words", f"{pulse.word_count} / 250")
    cols[3].metric("Validators passed", f"{passed} / {len(view.validators)}")
    st.caption(
        f"Window {pulse.window_start.isoformat()} to {pulse.window_end.isoformat()} · "
        f"report run {view.run_id} · created {dashboard.format_timestamp(view.created_at)} · "
        f"marker `{view.verified.marker}`"
    )

    st.subheader("Validators")
    for v in view.validators:
        icon = "✅" if v["passed"] else "❌"
        st.markdown(f"{icon} **{v['name']}** — {_md(v['detail'])}")

    st.subheader("Report preview")
    with st.container(border=True):
        st.markdown(pulse.body_markdown)
    with st.expander("Exact plain text appended to the Doc and used as the draft body"):
        st.code(view.verified.full_text, language=None)


def _themes_tab(board: dashboard.Dashboard, view: dashboard.DashboardPulse) -> None:
    pulse = view.pulse
    names = {t.theme_id: t.name for t in pulse.themes}

    st.subheader("Top themes")
    st.dataframe(
        [
            {
                "Rank": t.rank,
                "Theme": t.name,
                "Reviews": t.count,
                "Share": f"{t.share * 100:.1f}%",
                "Avg rating": f"{t.avg_rating:.1f}★" if t.avg_rating is not None else "—",
                "1–2★ share": f"{t.low_rating_share * 100:.1f}%",
                "Score": t.score,
            }
            for t in pulse.themes
        ],
        hide_index=True,
    )
    if board.analysis and board.analysis.ranking_formula:
        st.caption(f"Ranking: {board.analysis.ranking_formula}")
    for t in pulse.themes:
        st.markdown(f"**{_md(t.name)}** — {_md(t.description)}")

    st.subheader("Verified review quotes")
    st.caption(
        "Each quote is a verbatim span of a stored, PII-redacted review, re-verified when this "
        "page loaded. An ellipsis outside the quotation marks means the review continues."
    )
    for q in pulse.quotes:
        with st.container(border=True):
            st.markdown(_md(quote_display(q)))
            st.caption(
                f"{q.rating}★ · {q.created_at.date().isoformat()} · "
                f"{names.get(q.theme_id, q.theme_id)} · review `{q.review_id}`"
            )

    st.subheader("Action ideas")
    for i, a in enumerate(pulse.actions, 1):
        owner = f" ({_md(a.owner_area)})" if a.owner_area else ""
        st.markdown(f"{i}. **{_md(a.action)}**{owner}")
        if a.rationale:
            st.caption(a.rationale)


def _statistics_tab(board: dashboard.Dashboard, view: dashboard.DashboardPulse) -> None:
    pulse = view.pulse
    funnel = board.funnel
    for problem in board.problems:
        st.warning(f"Could not load: {problem}")

    cols = st.columns(4)
    cols[0].metric("Collected", pulse.n_collected)
    cols[1].metric("Analyzed", pulse.n_analyzed)
    if funnel:
        cols[2].metric("Excluded", funnel.total_dropped)
        cols[3].metric("Held from quoting", funnel.quote_ineligible)

    if funnel:
        st.subheader("Preprocessing funnel")
        st.caption(
            f"Preprocess run {funnel.run_id} · window {funnel.window_start} to "
            f"{funnel.window_end} · {funnel.reviews_redacted} review(s) had PII redacted"
        )
        st.dataframe(
            [
                {
                    "Step": s["step"],
                    "In": s["input"],
                    "Dropped": s["dropped"],
                    "Out": s["output"],
                    "Reasons": ", ".join(f"{k}: {v}" for k, v in s["reasons"].items()) or "—",
                }
                for s in funnel.steps
            ],
            hide_index=True,
        )
        if funnel.quote_holds:
            st.markdown("**Quote holds** (kept for analysis, never quoted)")
            st.dataframe(
                [{"Reason": k, "Reviews": v} for k, v in sorted(funnel.quote_holds.items())],
                hide_index=True,
            )
        if funnel.pii_redactions:
            st.markdown("**PII redactions by type**")
            st.dataframe(
                [{"Type": k, "Count": v} for k, v in sorted(funnel.pii_redactions.items())],
                hide_index=True,
            )

    st.subheader("Ratings in the top themes")
    st.dataframe(
        [
            {"Theme": t.name, **{f"{star}★": t.rating_counts.get(star, 0) for star in range(1, 6)}}
            for t in pulse.themes
        ],
        hide_index=True,
    )

    if board.analysis:
        a = board.analysis
        method = a.themes_method or "unknown"
        if a.provider:
            method += f" ({a.provider}, {a.model})"
        st.caption(f"Analysis run {a.run_id} · themes: {method}")
        if a.fallback_reason:
            st.warning(f"Fallback clustering was used: {a.fallback_reason}")


def _status_label(row: dashboard.DeliveryRow | None, blocked: bool) -> str:
    if row is not None and row.status == "succeeded":
        return row.status
    if blocked:
        return "blocked: week already delivered"
    return row.status if row else "not delivered"


def _delivery_tab(
    settings: Settings, board: dashboard.Dashboard, view: dashboard.DashboardPulse
) -> None:
    verified = view.verified
    week_blocks = view.week_blocks()

    st.subheader("Delivery status for this pulse")
    if board.ledger_error:
        st.error(f"Delivery ledger unreadable: {board.ledger_error}")
    else:
        st.dataframe(
            [
                {
                    "Target": TARGET_LABELS.get(target, target),
                    "Status": _status_label(row, target in week_blocks),
                    "When": dashboard.format_timestamp(row.timestamp) if row else "—",
                    "Delivery run": row.run_id if row else "—",
                    "Error code": (row.error_code or "—") if row else "—",
                }
                for target, row in board.delivery.items()
            ],
            hide_index=True,
        )
    if week_blocks:
        delivered = view.week_lock is not None and bool(view.week_lock.delivered)
        st.error(
            f"Blocked: a different version of the {verified.iso_week} pulse "
            + ("was already delivered" if delivered else "may already have been delivered")
            + ". Only one version per ISO week is delivered, so this pulse cannot be appended "
            "to the Doc or drafted, and this page has no override."
        )
        for target, reason in week_blocks.items():
            st.caption(f"{TARGET_LABELS.get(target, target)}: {reason}")
    else:
        for row in board.other_versions_delivered:
            st.info(
                f"A different version of the {verified.iso_week} pulse reached the "
                f"{TARGET_LABELS.get(row.target, row.target)} "
                f"({dashboard.format_timestamp(row.timestamp)}, run {row.run_id}); this "
                "pulse already reached part of the week, so it may finish its other targets."
            )
    st.caption(
        "Statuses come from the local ledger (DATA_DIR/runs/ledger.jsonl). On hosts with an "
        "ephemeral disk, such as Streamlit Community Cloud, the ledger resets on restart and "
        "duplicate protection is per session: check the Doc and Drafts before publishing."
    )

    _delivery_controls(settings, board, view, prefix="")

    if board.recovery_problem:
        st.warning(f"Cannot finish the earlier version: {board.recovery_problem}.")
    if (recovery := board.recovery) is not None:
        st.subheader(f"Finish the delivered {recovery.verified.iso_week} version")
        st.info(
            f"Version {recovery.verified.content_hash[:12]} (report run {recovery.run_id}) "
            "already reached some targets but not all. Delivering it again skips what it "
            "already reached and creates only what is missing."
        )
        _delivery_controls(settings, board, recovery, prefix="recovery_")


def _delivery_controls(
    settings: Settings, board: dashboard.Dashboard, view: dashboard.DashboardPulse, *,
    prefix: str,
) -> None:  # fmt: skip
    """Dry-run preview, confirmation and delivery of ``view``; widget keys use ``prefix``."""
    verified = view.verified
    content_hash = verified.content_hash
    state = st.session_state

    st.subheader("1. Preview (dry run)")
    st.caption(
        "No network calls. Shows the exact tool calls with the Doc ID and recipients masked."
    )
    if st.button("Preview delivery (dry run)", key=f"{prefix}preview"):
        with bind_run_id() as run_id:
            try:
                report = dashboard.preview_delivery(settings, view.directory, run_id=run_id)
            except DeliveryError as exc:
                state[f"{prefix}preview_error"] = f"Preview refused (run {run_id}): {exc}"
            else:
                state[f"{prefix}preview_result"] = _masked_json(report.as_dict())
                state[f"{prefix}previewed_hash"] = content_hash
                state.pop(f"{prefix}preview_error", None)
    if error := state.get(f"{prefix}preview_error"):
        st.error(error)
    previewed = state.get(f"{prefix}previewed_hash") == content_hash
    if previewed and f"{prefix}preview_result" in state:
        with st.expander("Dry-run result", expanded=True):
            st.code(state[f"{prefix}preview_result"], language="json")

    st.subheader("2. Deliver")
    blockers = dashboard.live_delivery_blockers(settings, board, view)
    if not previewed:
        blockers.append("preview this pulse with a dry run first")
    if blockers:
        st.warning("Live delivery is disabled: " + "; ".join(blockers) + ".")
    confirmed = st.checkbox(
        f"I reviewed the dry run and want to append the {verified.iso_week} pulse to the "
        "Google Doc and create one Gmail draft. No email will be sent.",
        key=f"{prefix}confirm_delivery",
        disabled=bool(blockers),
    )
    if st.button(
        "Append to Google Doc and create Gmail draft",
        key=f"{prefix}deliver",
        type="primary",
        disabled=bool(blockers) or not confirmed,
    ):
        with bind_run_id() as run_id, st.spinner("Delivering through the MCP server…"):
            try:
                report = dashboard.deliver(settings, view.directory, run_id=run_id)
            except (ConfigError, DeliveryError) as exc:
                state[f"{prefix}delivery_result"] = ("error", f"Refused (run {run_id}): {exc}")
            else:
                level = "success" if report.ok else "error"
                state[f"{prefix}delivery_result"] = (level, _masked_json(report.as_dict()))
        state.pop(f"{prefix}confirm_delivery", None)
        st.rerun()
    if result := state.get(f"{prefix}delivery_result"):
        level, body = result
        if level == "success":
            st.success("Delivered. Check the Doc and the Drafts folder; nothing was sent.")
            st.code(body, language="json")
        elif body.startswith("Refused"):
            st.error(body)
        else:
            st.error("Not fully delivered. The MCP responses are below.")
            st.code(body, language="json")


def _health_section(settings: Settings) -> None:
    st.subheader("MCP server health")
    st.caption(
        "Read-only: handshake, tool list, contract, and an authentication probe. No tool is called."
    )
    if st.button(
        "Check MCP server (read-only)", key="health", disabled=settings.mcp_server_url is None
    ):
        with bind_run_id(), st.spinner("Checking the MCP server…"):
            try:
                health = dashboard.check_mcp(settings)
            except (ConfigError, DeliveryError) as exc:
                st.session_state["health_result"] = ("error", str(exc))
            else:
                ok = health.healthy and health.security_gate_passed
                st.session_state["health_result"] = (
                    "success" if ok else "warning",
                    _masked_json(health.as_dict()),
                )
    if health_result := st.session_state.get("health_result"):
        level, body = health_result
        if level == "error":
            st.error(body)
        else:
            (st.success if level == "success" else st.warning)(
                "Healthy, security gate passed."
                if level == "success"
                else "Unhealthy, or the security gate failed: live delivery will be refused."
            )
            st.code(body, language="json")


def _configuration_tab(settings: Settings) -> None:
    st.subheader("Readiness")
    for label, ok, detail in _readiness(settings):
        (st.success if ok else st.warning)(f"{label}: {detail}")
    st.subheader("Configuration (secrets are shown only as set / not set)")
    st.table([{"Variable": k, "Value": v} for k, v in settings.public_summary().items()])
    st.subheader("MCP tool policy")
    st.write(f"Allowed: `{', '.join(sorted(ALLOWED_TOOLS))}`")
    st.write(f"Blocked: `{', '.join(sorted(FORBIDDEN_TOOLS))}`")


def main() -> None:
    st.set_page_config(page_title="Groww Review Pulse", layout="wide")
    st.title("Groww Review Pulse Agent")
    st.caption(f"v{__version__} · Google Play reviews only · weekly product-feedback pulse")

    try:
        settings = load_settings()
    except ConfigError as exc:
        st.error(f"Configuration error: {exc}")
        st.stop()
        return

    configure_logging(settings.log_level, settings.log_format, settings.secret_values())

    st.info(
        f"Delivery mode: **{settings.delivery_mode.value}**. "
        "This app only appends to a Google Doc and creates Gmail drafts — it never sends email."
    )

    with st.spinner("Loading and re-verifying the latest weekly pulse…"):
        try:
            board = dashboard.load_dashboard(settings)
        except OSError as exc:
            st.error(f"Could not read DATA_DIR ({settings.data_dir}): {type(exc).__name__}")
            st.stop()
            return

    _rejected(board)
    view = board.pulse
    if view is None:
        _empty_state()
        _configuration_tab(settings)
        return
    if view.synthetic:
        st.warning("SYNTHETIC TEST DATA — this pulse is not for publication.")

    pulse_tab, themes_tab, stats_tab, delivery_tab, config_tab = st.tabs(
        ["Weekly pulse", "Themes, quotes & actions", "Review statistics", "Delivery", "Settings"]
    )
    with pulse_tab:
        _pulse_tab(view)
    with themes_tab:
        _themes_tab(board, view)
    with stats_tab:
        _statistics_tab(board, view)
    with delivery_tab:
        _delivery_tab(settings, board, view)
        _health_section(settings)
    with config_tab:
        _configuration_tab(settings)


main()
