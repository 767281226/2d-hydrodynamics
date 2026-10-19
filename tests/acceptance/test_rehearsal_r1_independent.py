"""Coordinator-owned counterexamples; no solver, database or real input reads."""

import inspect
from pathlib import Path

import pytest

from hydrodynamics import rehearsal_contract as rc


ROOT = Path(__file__).resolve().parents[2]


def baseline():
    scenario = rc.load_scenario_document(ROOT / 'examples/rehearsal/scenario_synthetic_closed_box.yaml')
    report = rc.evaluate_scenario(scenario)
    assert report.can_compute
    envelope = rc.ExecutionEnvelope.model_validate({
        'format': 'rehearsal-execution-envelope-v1',
        'tenant_id': 'tenant-A', 'project_id': 'project-A',
        'scenario_id': 'scenario-A', 'scenario_version_id': 'version-A',
        'platform_run_id': 'platform-run-A', 'attempt_id': 'attempt-A', 'attempt_no': 1,
        'script_id': 'script-assigned-A', 'local_run_id': 'script-assigned-A',
        'model_case_id': report.resolved_model_case_id,
        'input_sha256': report.resolved_input_sha256, 'engine_mode': scenario.engine_mode,
    })
    return scenario, report, envelope


def validate(envelope, scenario, report):
    context = dict(trusted_tenant_id='tenant-A', trusted_project_id='project-A',
                   trusted_scenario_id='scenario-A', trusted_scenario_version_id='version-A')
    # Preserve the same trusted assignment across pre- and post-repair API versions.
    expected = dict(trusted_platform_run_id='platform-run-A', trusted_attempt_id='attempt-A',
                    trusted_attempt_no=1, trusted_script_id='script-assigned-A')
    parameters = inspect.signature(rc.validate_execution_envelope).parameters
    context.update({name: value for name, value in expected.items() if name in parameters})
    rc.validate_execution_envelope(envelope, scenario, report, **context)


def test_original_resolved_execution_is_accepted():
    scenario, report, envelope = baseline()
    validate(envelope, scenario, report)


def test_stale_preflight_cannot_authorize_changed_rainfall():
    scenario, report, envelope = baseline()
    raw = scenario.model_dump(mode='json')
    raw['rainfall'] = {'mode': 'UNKNOWN'}
    changed = rc.ScenarioDraft.model_validate(raw)
    assert not rc.evaluate_scenario(changed).can_compute
    with pytest.raises(ValueError):
        validate(envelope, changed, report)


@pytest.mark.parametrize('patch', [
    {'script_id': 'worker-replaced-script', 'local_run_id': 'worker-replaced-script'},
    {'platform_run_id': 'another-platform-run'},
    {'attempt_id': 'stale-attempt', 'attempt_no': 99},
])
def test_worker_cannot_replace_server_execution_assignment(patch):
    scenario, report, envelope = baseline()
    altered = rc.ExecutionEnvelope.model_validate({**envelope.model_dump(mode='json'), **patch})
    with pytest.raises(ValueError):
        validate(altered, scenario, report)


def test_self_asserted_preflight_cannot_bypass_untrusted_profile():
    scenario, _, envelope = baseline()
    raw = scenario.model_dump(mode='json')
    raw['input_profile_ref'] = {'profile_id': 'untrusted-real-domain', 'profile_version': '1.0'}
    untrusted = rc.ScenarioDraft.model_validate(raw)
    actual = rc.evaluate_scenario(untrusted)
    assert not actual.can_compute
    forged = rc.ScenarioPreflightReport.model_validate({
        **actual.model_dump(mode='json'), 'can_compute': True,
        'resolved_input_sha256': 'a' * 64,
        'resolved_model_case_id': envelope.model_case_id,
    })
    altered = rc.ExecutionEnvelope.model_validate({**envelope.model_dump(mode='json'), 'input_sha256': 'a' * 64})
    with pytest.raises(ValueError):
        validate(altered, untrusted, forged)


@pytest.mark.parametrize('duration,step', [('120', 60), (120, '60')])
def test_public_output_axis_api_rejects_numeric_strings(duration, step):
    with pytest.raises(ValueError):
        rc.normalize_output_times(duration, step)
