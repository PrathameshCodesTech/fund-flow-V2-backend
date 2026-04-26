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


# ---------------------------------------------------------------------------
# Tests: multiple template variants per module+scope (Enterprise Variants)
# ---------------------------------------------------------------------------

class TestMultipleTemplateVariants:
    """Tests that validate multiple WorkflowTemplate variants per module+scope."""

    def test_can_create_two_templates_at_same_scope_with_different_codes(self, entity, user):
        """Two invoice templates can coexist at the same scope node when codes differ."""
        t1 = WorkflowTemplate.objects.create(
            name="Standard 6 Step", module="invoice", scope_node=entity, created_by=user, code="standard-6"
        )
        t2 = WorkflowTemplate.objects.create(
            name="Fast Track 4 Step", module="invoice", scope_node=entity, created_by=user, code="fast-track-4"
        )
        assert WorkflowTemplate.objects.filter(module="invoice", scope_node=entity).count() == 2
        assert t1.code != t2.code

    def test_cannot_create_duplicate_code_for_same_module_scope(self, entity, user):
        """Creating a second template with the same code at the same scope raises IntegrityError."""
        from django.db import IntegrityError
        WorkflowTemplate.objects.create(
            name="Standard", module="invoice", scope_node=entity, created_by=user, code="standard"
        )
        with pytest.raises(IntegrityError):
            WorkflowTemplate.objects.create(
                name="Standard Duplicate", module="invoice", scope_node=entity, created_by=user, code="standard"
            )

    def test_can_publish_versions_on_each_template_independently(self, entity, user):
        """Each template variant can have its own published version."""
        from apps.workflow.services import publish_template_version
        t1 = WorkflowTemplate.objects.create(
            name="Standard", module="invoice", scope_node=entity, created_by=user, code="standard"
        )
        t2 = WorkflowTemplate.objects.create(
            name="Fast Track", module="invoice", scope_node=entity, created_by=user, code="fast-track"
        )
        v1 = WorkflowTemplateVersion.objects.create(template=t1, version_number=1, status=VersionStatus.DRAFT)
        v2 = WorkflowTemplateVersion.objects.create(template=t2, version_number=1, status=VersionStatus.DRAFT)

        publish_template_version(v1, published_by=user)
        publish_template_version(v2, published_by=user)

        v1.refresh_from_db()
        v2.refresh_from_db()
        assert v1.status == VersionStatus.PUBLISHED
        assert v2.status == VersionStatus.PUBLISHED

    def test_publishing_v2_on_template_a_does_not_archive_template_b_published_version(self, entity, user):
        """
        Publishing a new version on Template A archives Template A's old published version only.
        Template B's published version is unaffected.
        """
        from apps.workflow.services import publish_template_version
        t_a = WorkflowTemplate.objects.create(
            name="Template A", module="invoice", scope_node=entity, created_by=user, code="template-a"
        )
        t_b = WorkflowTemplate.objects.create(
            name="Template B", module="invoice", scope_node=entity, created_by=user, code="template-b"
        )
        v_a1 = WorkflowTemplateVersion.objects.create(template=t_a, version_number=1, status=VersionStatus.DRAFT)
        v_b1 = WorkflowTemplateVersion.objects.create(template=t_b, version_number=1, status=VersionStatus.DRAFT)

        publish_template_version(v_a1, published_by=user)
        publish_template_version(v_b1, published_by=user)

        # Now publish v2 on Template A
        v_a2 = WorkflowTemplateVersion.objects.create(template=t_a, version_number=2, status=VersionStatus.DRAFT)
        publish_template_version(v_a2, published_by=user)

        v_a1.refresh_from_db()
        v_a2.refresh_from_db()
        v_b1.refresh_from_db()

        assert v_a1.status == VersionStatus.ARCHIVED, "Template A v1 should be archived"
        assert v_a2.status == VersionStatus.PUBLISHED, "Template A v2 should be published"
        assert v_b1.status == VersionStatus.PUBLISHED, "Template B v1 must not be affected"

    def test_resolve_uses_default_template_when_multiple_variants_exist(self, entity, company, user):
        """
        When multiple variants exist, resolve_workflow_template_version picks the is_default one.
        """
        _activate_module(company)
        t_default = WorkflowTemplate.objects.create(
            name="Standard", module="invoice", scope_node=entity,
            created_by=user, code="standard", is_default=True,
        )
        t_other = WorkflowTemplate.objects.create(
            name="Fast Track", module="invoice", scope_node=entity,
            created_by=user, code="fast-track", is_default=False,
        )
        v_default = WorkflowTemplateVersion.objects.create(
            template=t_default, version_number=1, status=VersionStatus.PUBLISHED
        )
        WorkflowTemplateVersion.objects.create(
            template=t_other, version_number=1, status=VersionStatus.PUBLISHED
        )

        result = resolve_workflow_template_version("invoice", entity)
        assert result.pk == v_default.pk, "Default template's version must be returned"

    def test_resolve_single_non_default_template_returned_without_default(self, entity, company, user):
        """
        With no default set and exactly one active template having a published version,
        that version is returned automatically.
        """
        _activate_module(company)
        t = WorkflowTemplate.objects.create(
            name="Only Template", module="invoice", scope_node=entity,
            created_by=user, code="only", is_default=False,
        )
        version = WorkflowTemplateVersion.objects.create(
            template=t, version_number=1, status=VersionStatus.PUBLISHED
        )

        result = resolve_workflow_template_version("invoice", entity)
        assert result.pk == version.pk

    def test_resolve_raises_when_multiple_non_default_published_variants(self, entity, company, user):
        """
        When multiple non-default active templates each have a published version,
        automatic resolution raises WorkflowNotConfiguredError.
        """
        _activate_module(company)
        for code in ("variant-a", "variant-b"):
            t = WorkflowTemplate.objects.create(
                name=f"Variant {code}", module="invoice", scope_node=entity,
                created_by=user, code=code, is_default=False,
            )
            WorkflowTemplateVersion.objects.create(
                template=t, version_number=1, status=VersionStatus.PUBLISHED
            )

        with pytest.raises(WorkflowNotConfiguredError, match="Explicit workflow selection is required"):
            resolve_workflow_template_version("invoice", entity)

    def test_inactive_template_is_ignored_by_resolve(self, entity, company, user):
        """An inactive template is not considered by resolve, even if it has a published version."""
        _activate_module(company)
        t_inactive = WorkflowTemplate.objects.create(
            name="Inactive", module="invoice", scope_node=entity,
            created_by=user, code="inactive", is_active=False,
        )
        WorkflowTemplateVersion.objects.create(
            template=t_inactive, version_number=1, status=VersionStatus.PUBLISHED
        )

        with pytest.raises(WorkflowNotConfiguredError):
            resolve_workflow_template_version("invoice", entity)

    def test_resolve_falls_back_to_ancestor_when_default_has_no_published_version(
        self, entity, company, user
    ):
        """
        If the default template at the subject node has no published version,
        resolution walks up to find a published version at an ancestor node.
        """
        _activate_module(company)
        t_entity = WorkflowTemplate.objects.create(
            name="Entity Default", module="invoice", scope_node=entity,
            created_by=user, code="entity-default", is_default=True,
        )
        # Entity default template has only a DRAFT — no published version
        WorkflowTemplateVersion.objects.create(
            template=t_entity, version_number=1, status=VersionStatus.DRAFT
        )
        # Company has a published template
        _, company_version = _published_template(company, user)

        result = resolve_workflow_template_version("invoice", entity)
        assert result.pk == company_version.pk
