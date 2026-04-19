import pytest
from apps.core.models import Organization, ScopeNode, NodeType
from apps.core.services import (
    build_node_path,
    get_node_depth,
    update_descendant_paths,
    get_subtree_nodes,
    get_ancestors,
)


@pytest.fixture
def org(db):
    return Organization.objects.create(name="Test Org", code="testorg")


@pytest.fixture
def company(org):
    return ScopeNode.objects.create(
        org=org, parent=None, name="HQ", code="hq",
        node_type=NodeType.COMPANY,
        path="/testorg/hq", depth=0,
    )


@pytest.fixture
def entity(org, company):
    return ScopeNode.objects.create(
        org=org, parent=company, name="Entity A", code="entity-a",
        node_type=NodeType.ENTITY,
        path="/testorg/hq/entity-a", depth=1,
    )


@pytest.fixture
def branch(org, entity):
    return ScopeNode.objects.create(
        org=org, parent=entity, name="Branch X", code="branch-x",
        node_type=NodeType.BRANCH,
        path="/testorg/hq/entity-a/branch-x", depth=2,
    )


class TestBuildNodePath:
    def test_root_node(self, org):
        path = build_node_path(parent=None, org=org, code="hq")
        assert path == "/testorg/hq"

    def test_child_node(self, org, company):
        path = build_node_path(parent=company, org=org, code="entity-a")
        assert path == "/testorg/hq/entity-a"

    def test_deep_node(self, org, entity):
        path = build_node_path(parent=entity, org=org, code="branch-x")
        assert path == "/testorg/hq/entity-a/branch-x"


class TestGetNodeDepth:
    def test_root_depth(self):
        assert get_node_depth(parent=None) == 0

    def test_child_depth(self, company):
        assert get_node_depth(parent=company) == 1

    def test_grandchild_depth(self, entity):
        assert get_node_depth(parent=entity) == 2


class TestGetAncestors:
    def test_no_ancestors_for_root(self, company):
        ancestors = list(get_ancestors(company))
        assert ancestors == []

    def test_single_ancestor(self, entity, company):
        ancestors = list(get_ancestors(entity))
        assert len(ancestors) == 1
        assert ancestors[0].pk == company.pk

    def test_multiple_ancestors_ordered(self, branch, company, entity):
        ancestors = list(get_ancestors(branch))
        assert len(ancestors) == 2
        assert ancestors[0].pk == company.pk
        assert ancestors[1].pk == entity.pk


class TestGetSubtreeNodes:
    def test_includes_self(self, company):
        nodes = list(get_subtree_nodes(company))
        pks = [n.pk for n in nodes]
        assert company.pk in pks

    def test_includes_all_descendants(self, company, entity, branch):
        nodes = list(get_subtree_nodes(company))
        pks = [n.pk for n in nodes]
        assert entity.pk in pks
        assert branch.pk in pks

    def test_leaf_subtree_is_just_self(self, branch):
        nodes = list(get_subtree_nodes(branch))
        assert len(nodes) == 1
        assert nodes[0].pk == branch.pk


class TestUpdateDescendantPaths:
    def test_updates_descendants_on_path_change(self, db, org, company, entity, branch):
        old_path = company.path
        company.code = "headquarters"
        company.path = "/testorg/headquarters"
        company.save()
        update_descendant_paths(company, old_path)

        entity.refresh_from_db()
        branch.refresh_from_db()

        assert entity.path == "/testorg/headquarters/entity-a"
        assert branch.path == "/testorg/headquarters/entity-a/branch-x"
