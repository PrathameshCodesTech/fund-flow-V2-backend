from rest_framework import serializers

from apps.campaigns.models import Campaign, CampaignDocument
from apps.budgets.models import BudgetStatus


# ---------------------------------------------------------------------------
# CampaignDocument
# ---------------------------------------------------------------------------

class CampaignDocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = CampaignDocument
        fields = (
            "id", "campaign", "title", "file_url", "document_type",
            "uploaded_by", "created_at",
        )
        read_only_fields = ("id", "created_at")


class CampaignDocumentCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = CampaignDocument
        fields = ("campaign", "title", "file_url", "document_type")


# ---------------------------------------------------------------------------
# Campaign (read)
# ---------------------------------------------------------------------------

class CampaignSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(
        source="category.name", read_only=True, allow_null=True
    )
    subcategory_name = serializers.CharField(
        source="subcategory.name", read_only=True, allow_null=True
    )
    scope_node_name = serializers.CharField(
        source="scope_node.name", read_only=True
    )
    budget_variance_request_id = serializers.PrimaryKeyRelatedField(
        source="budget_variance_request", read_only=True, allow_null=True
    )

    class Meta:
        model = Campaign
        fields = (
            "id", "org", "scope_node", "scope_node_name",
            "name", "code", "description", "campaign_type",
            "start_date", "end_date",
            "requested_amount", "approved_amount", "currency",
            "category", "category_name",
            "subcategory", "subcategory_name",
            "budget", "budget_variance_request_id",
            "status",
            "created_by", "created_at", "updated_at",
        )
        read_only_fields = (
            "id", "approved_amount", "status",
            "created_by", "created_at", "updated_at",
        )


# ---------------------------------------------------------------------------
# Campaign (write)
# ---------------------------------------------------------------------------

class CampaignCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Campaign
        fields = (
            "org", "scope_node", "name", "code", "description",
            "campaign_type", "start_date", "end_date",
            "requested_amount", "currency",
            "category", "subcategory", "budget",
        )

    def validate(self, data):
        subcategory = data.get("subcategory")
        category = data.get("category")
        if subcategory and category and subcategory.category_id != category.id:
            raise serializers.ValidationError({
                "subcategory": "Subcategory does not belong to the selected category."
            })
        if data.get("start_date") and data.get("end_date"):
            if data["start_date"] >= data["end_date"]:
                raise serializers.ValidationError({
                    "end_date": "end_date must be after start_date."
                })
        budget = data.get("budget")
        if budget:
            if budget.status != BudgetStatus.ACTIVE:
                raise serializers.ValidationError({
                    "budget": f"Budget is not active (status: {budget.status})."
                })
            org = data.get("org")
            if org and budget.org_id and budget.org_id != org.id:
                raise serializers.ValidationError({
                    "budget": "Budget does not belong to the selected organisation."
                })
            scope_node = data.get("scope_node")
            if scope_node and budget.scope_node_id and budget.scope_node_id != scope_node.id:
                raise serializers.ValidationError({
                    "budget": "Budget scope node does not match the campaign scope node."
                })
            if category and budget.category_id and budget.category_id != category.id:
                raise serializers.ValidationError({
                    "budget": "Budget category does not match the campaign category."
                })
            if subcategory and budget.subcategory_id and budget.subcategory_id != subcategory.id:
                raise serializers.ValidationError({
                    "budget": "Budget subcategory does not match the campaign subcategory."
                })
        return data


class CampaignUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Campaign
        fields = (
            "name", "description", "campaign_type",
            "start_date", "end_date", "requested_amount", "currency",
            "category", "subcategory", "budget",
        )

    def validate(self, data):
        subcategory = data.get("subcategory", getattr(self.instance, "subcategory", None))
        category = data.get("category", getattr(self.instance, "category", None))
        if subcategory and category and subcategory.category_id != category.id:
            raise serializers.ValidationError({
                "subcategory": "Subcategory does not belong to the selected category."
            })
        start_date = data.get("start_date", getattr(self.instance, "start_date", None))
        end_date = data.get("end_date", getattr(self.instance, "end_date", None))
        if start_date and end_date and start_date >= end_date:
            raise serializers.ValidationError({
                "end_date": "end_date must be after start_date."
            })
        budget = data.get("budget", getattr(self.instance, "budget", None))
        if budget and "budget" in data:
            if budget.status != BudgetStatus.ACTIVE:
                raise serializers.ValidationError({
                    "budget": f"Budget is not active (status: {budget.status})."
                })
            org = getattr(self.instance, "org", None)
            if org and budget.org_id and budget.org_id != org.id:
                raise serializers.ValidationError({
                    "budget": "Budget does not belong to the campaign's organisation."
                })
            scope_node = getattr(self.instance, "scope_node", None)
            if scope_node and budget.scope_node_id and budget.scope_node_id != scope_node.id:
                raise serializers.ValidationError({
                    "budget": "Budget scope node does not match the campaign scope node."
                })
            if category and budget.category_id and budget.category_id != category.id:
                raise serializers.ValidationError({
                    "budget": "Budget category does not match the campaign category."
                })
            if subcategory and budget.subcategory_id and budget.subcategory_id != subcategory.id:
                raise serializers.ValidationError({
                    "budget": "Budget subcategory does not match the campaign subcategory."
                })
        return data


# ---------------------------------------------------------------------------
# Action request serializers
# ---------------------------------------------------------------------------

class ReviewBudgetVarianceSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(choices=["approved", "rejected"])
    review_note = serializers.CharField(required=False, default="", allow_blank=True)


class CancelCampaignSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, default="", allow_blank=True)
