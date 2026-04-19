import pytest
from apps.core.models import Organization, ScopeNode, NodeType
from apps.modules.models import ModuleActivation, ModuleType
from apps.modules.services import resolve_module_activation


@pytest.fixture
def org(db):
    return Organization.objects.create(name="Org", code="org")


@pytest.fixture
def company(org):
    return ScopeNode.objects.create(
        org=org, parent=None, name="HQ", code="hq",
        node_type=NodeType.COMPANY, path="/org/hq", depth=0,
    )


@pytest.fixture
def entity(org, company):
    return ScopeNode.objects.create(
        org=org, parent=company, name="Entity A", code="ea",
        node_type=NodeType.ENTITY, path="/org/hq/ea", depth=1,
    )


@pytest.fixture
def branch(org, entity):
    return ScopeNode.objects.create(
        org=org, parent=entity, name="Branch X", code="bx",
        node_type=NodeType.BRANCH, path="/org/hq/ea/bx", depth=2,
    )


class TestModuleResolver:
    def test_default_off_when_no_rows(self, branch):
        result = resolve_module_activation(ModuleType.INVOICE, branch)
        assert result is False

    def test_override_at_exact_node(self, branch):
        ModuleActivation.objects.create(
            module=ModuleType.INVOICE, scope_node=branch,
            is_active=True, override_parent=True,
        )
        result = resolve_module_activation(ModuleType.INVOICE, branch)
        assert result is True

    def test_override_false_is_skipped(self, branch, entity):
        # branch has override_parent=False → informational only
        ModuleActivation.objects.create(
            module=ModuleType.INVOICE, scope_node=branch,
            is_active=True, override_parent=False,
        )
        # entity has override_parent=True but is_active=False
        ModuleActivation.objects.create(
            module=ModuleType.INVOICE, scope_node=entity,
            is_active=False, override_parent=True,
        )
        result = resolve_module_activation(ModuleType.INVOICE, branch)
        assert result is False

    def test_ancestor_override_true_wins(self, branch, company):
        ModuleActivation.objects.create(
            module=ModuleType.INVOICE, scope_node=company,
            is_active=True, override_parent=True,
        )
        result = resolve_module_activation(ModuleType.INVOICE, branch)
        assert result is True

    def test_nearest_ancestor_override_wins(self, branch, entity, company):
        # entity overrides → should win over company
        ModuleActivation.objects.create(
            module=ModuleType.INVOICE, scope_node=entity,
            is_active=False, override_parent=True,
        )
        ModuleActivation.objects.create(
            module=ModuleType.INVOICE, scope_node=company,
            is_active=True, override_parent=True,
        )
        result = resolve_module_activation(ModuleType.INVOICE, branch)
        assert result is False

    def test_different_module_not_affected(self, branch):
        ModuleActivation.objects.create(
            module=ModuleType.INVOICE, scope_node=branch,
            is_active=True, override_parent=True,
        )
        result = resolve_module_activation(ModuleType.CAMPAIGN, branch)
        assert result is False
