"""
Tests for Gap #1: walk-up template resolution with module activation gate.
"""
import pytest
from apps.core.models import Organization, ScopeNode, NodeType
from apps.users.models import User
from apps.modules.models import ModuleActivation, ModuleType
from apps.workflow.models import (
    WorkflowTemplate,
    WorkflowTemplateVersion,
    VersionStatus,
)
from apps.workflow.services import (
    resolve_workflow_template_version,
    ModuleInactiveError,
    WorkflowNotConfiguredError,
)


@pytest.fixture
def org(db):
    return Organization.objects.create(name="Org", code="tr-org")


@pytest.fixture
def company(org):
    return ScopeNode.objects.create(
        org=org, parent=None, name="HQ", code="hq",
        node_type=NodeType.COMPANY, path="/tr-org/hq", depth=0,
    )


@pytest.fixture
def entity(org, company):
    return ScopeNode.objects.create(
        org=org, parent=company, name="Entity A", code="ea",
        node_type=NodeType.ENTITY, path="/tr-org/hq/ea", depth=1,
    )


@pytest.fixture
def branch(org, entity):
    return ScopeNode.objects.create(
        org=org, parent=entity, name="Branch X", code="bx",
        node_type=NodeType.BRANCH, path="/tr-org/hq/ea/bx", depth=2,
    )


@pytest.fixture
def user(db):
    return User.objects.create_user(email="tr@example.com", password="pass")


def _activate_module(node, module=ModuleType.INVOICE):
    """Helper: set module active with override at the given node."""
    ModuleActivation.objects.create(
        module=module, scope_node=node, is_active=True, override_parent=True
    )


def _deactivate_module(node, module=ModuleType.INVOICE):
    """Helper: set module explicitly inactive with override."""
    ModuleActivation.objects.create(
        module=module, scope_node=node, is_active=False, override_parent=True
    )


def _published_template(node, user, module="invoice", version_number=1):
    """Helper: create a WorkflowTemplate with a published version at node."""
    template = WorkflowTemplate.objects.create(
        name=f"WF @ {node.code}", module=module, scope_node=node, created_by=user
    )
    version = WorkflowTemplateVersion.objects.create(
        template=template, version_number=version_number, status=VersionStatus.PUBLISHED
    )
    return template, version


class TestTemplateResolutionModuleGate:
    def test_module_inactive_raises_error(self, entity):
        """Module inactive → ModuleInactiveError before any template lookup."""
        _deactivate_module(entity)
        with pytest.raises(ModuleInactiveError):
            resolve_workflow_template_version("invoice", entity)

    def test_no_module_activation_row_raises_error(self, entity):
        """No activation row → defaults to OFF → ModuleInactiveError."""
        with pytest.raises(ModuleInactiveError):
            resolve_workflow_template_version("invoice", entity)

    def test_module_active_at_ancestor_passes_gate(self, entity, company, user):
        """Module active via ancestor override is sufficient to pass the gate."""
        _activate_module(company)
        _published_template(entity, user)
        # Should not raise ModuleInactiveError
        version = resolve_workflow_template_version("invoice", entity)
        assert version is not None


class TestTemplateResolutionWalkUp:
    def test_exact_node_match_wins(self, entity, company, user):
        """Template at subject node is preferred over any ancestor template."""
        _activate_module(company)
        _, entity_version = _published_template(entity, user, version_number=1)
        _, company_version = _published_template(company, user, version_number=1)

        result = resolve_workflow_template_version("invoice", entity)
        assert result.pk == entity_version.pk

    def test_parent_template_fallback_when_no_exact_match(self, entity, company, user):
        """No template at subject → fallback to parent template."""
        _activate_module(company)
        _, company_version = _published_template(company, user)
        # No template at entity

        result = resolve_workflow_template_version("invoice", entity)
        assert result.pk == company_version.pk

    def test_template_at_grandparent_found_via_walkup(self, branch, entity, company, user):
        """Walk-up reaches grandparent when neither subject nor parent have a template."""
        _activate_module(company)
        _, company_version = _published_template(company, user)
        # No templates at entity or branch

        result = resolve_workflow_template_version("invoice", branch)
        assert result.pk == company_version.pk

    def test_nearest_template_wins_over_grandparent(self, branch, entity, company, user):
        """Entity template should win over company template when walking up from branch."""
        _activate_module(company)
        _, entity_version = _published_template(entity, user, version_number=1)
        _, company_version = _published_template(company, user, version_number=1)

        result = resolve_workflow_template_version("invoice", branch)
        assert result.pk == entity_version.pk

    def test_no_template_anywhere_raises_not_configured(self, entity, company, user):
        """No template in the entire walk-up → WorkflowNotConfiguredError."""
        _activate_module(company)

        with pytest.raises(WorkflowNotConfiguredError):
            resolve_workflow_template_version("invoice", entity)

    def test_template_with_no_published_version_is_skipped(self, entity, company, user):
        """A template that exists but has only DRAFT versions must be skipped."""
        _activate_module(company)
        # entity has a template but only a draft version — should be skipped
        entity_template = WorkflowTemplate.objects.create(
            name="Entity WF", module="invoice", scope_node=entity, created_by=user
        )
        WorkflowTemplateVersion.objects.create(
            template=entity_template, version_number=1, status=VersionStatus.DRAFT
        )
        # company has a published template — should be found instead
        _, company_version = _published_template(company, user)

        result = resolve_workflow_template_version("invoice", entity)
        assert result.pk == company_version.pk

    def test_different_module_not_matched(self, entity, company, user):
        """Template for a different module is not returned."""
        _activate_module(company, ModuleType.INVOICE)
        _published_template(entity, user, module="campaign")

        with pytest.raises(WorkflowNotConfiguredError):
            resolve_workflow_template_version("invoice", entity)
