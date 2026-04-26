# Manual Expenses API Serializers

from rest_framework import serializers
from apps.manual_expenses.models import ManualExpenseEntry, ManualExpenseAttachment


class ManualExpenseAttachmentSerializer(serializers.ModelSerializer):
    download_url = serializers.SerializerMethodField()

    class Meta:
        model = ManualExpenseAttachment
        fields = [
            "id",
            "expense_entry",
            "title",
            "document_type",
            "file_name",
            "download_url",
            "uploaded_by",
            "created_at",
        ]
        read_only_fields = ["id", "file_name", "download_url", "uploaded_by", "created_at"]

    def get_download_url(self, obj):
        if obj.file:
            request = self.context.get("request")
            if request:
                return request.build_absolute_uri(obj.file.url)
        return None

    def to_representation(self, obj):
        rep = super().to_representation(obj)
        rep["file_name"] = obj.file.name.split("/")[-1] if obj.file else ""
        return rep


class ManualExpenseListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for list view."""
    attachment_count = serializers.IntegerField(read_only=True)
    budget_name = serializers.CharField(source="budget.name", read_only=True, default="")
    category_name = serializers.CharField(source="category.name", read_only=True, default="")
    subcategory_name = serializers.CharField(source="subcategory.name", read_only=True, default="")
    vendor_name = serializers.CharField(read_only=True, default="")

    class Meta:
        model = ManualExpenseEntry
        fields = [
            "id",
            "org",
            "scope_node",
            "status",
            "payment_method",
            "vendor_name",
            "reference_number",
            "expense_date",
            "amount",
            "currency",
            "budget_name",
            "category_name",
            "subcategory_name",
            "description",
            "attachment_count",
            "submitted_at",
            "settled_at",
            "cancelled_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class ManualExpenseDetailSerializer(serializers.ModelSerializer):
    """Full serializer for create/edit/retrieve."""
    attachments = ManualExpenseAttachmentSerializer(many=True, read_only=True)
    attachment_count = serializers.IntegerField(read_only=True)
    budget_name = serializers.CharField(source="budget.name", read_only=True, default="")
    category_name = serializers.CharField(source="category.name", read_only=True, default="")
    subcategory_name = serializers.CharField(source="subcategory.name", read_only=True, default="")
    vendor_name = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = ManualExpenseEntry
        fields = [
            "id",
            "org",
            "scope_node",
            "created_by",
            "created_by_name",
            "status",
            "payment_method",
            "vendor_name",
            "vendor",
            "reference_number",
            "expense_date",
            "amount",
            "currency",
            "budget",
            "budget_name",
            "budget_line",
            "category",
            "category_name",
            "subcategory",
            "subcategory_name",
            "description",
            "source_note",
            "attachment_count",
            "attachments",
            "submitted_at",
            "settled_at",
            "cancelled_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "org",
            "created_by",
            "created_by_name",
            "status",
            "submitted_at",
            "settled_at",
            "cancelled_at",
            "created_at",
            "updated_at",
        ]

    def get_vendor_name(self, obj):
        return obj.vendor.vendor_name if obj.vendor else obj.vendor_name

    def get_created_by_name(self, obj):
        if obj.created_by:
            full_name = obj.created_by.get_full_name()
            return full_name if full_name else obj.created_by.username
        return ""

    def to_representation(self, obj):
        # Pass request to nested attachment serializer
        rep = super().to_representation(obj)
        rep["_is_editable"] = obj.status == "draft"
        return rep


class ManualExpenseCreateSerializer(serializers.ModelSerializer):
    """Used for create and update of draft expenses."""

    class Meta:
        model = ManualExpenseEntry
        fields = [
            "scope_node",
            "payment_method",
            "vendor_name",
            "vendor",
            "reference_number",
            "expense_date",
            "amount",
            "currency",
            "budget",
            "budget_line",
            "category",
            "subcategory",
            "description",
            "source_note",
        ]
        extra_kwargs = {
            "vendor_name": {"required": False},
            "vendor": {"required": False},
            "reference_number": {"required": False},
            "budget_line": {"required": False},
            "description": {"required": False},
            "source_note": {"required": False},
        }

    def validate(self, attrs):
        scope_node = attrs.get("scope_node") or getattr(self.instance, "scope_node", None)
        budget = attrs.get("budget") or getattr(self.instance, "budget", None)
        budget_line = attrs.get("budget_line") or getattr(self.instance, "budget_line", None)
        category = attrs.get("category") or getattr(self.instance, "category", None)
        subcategory = attrs.get("subcategory") or getattr(self.instance, "subcategory", None)

        actionable_scope_ids = self.context.get("actionable_scope_ids", [])
        if scope_node and actionable_scope_ids and scope_node.id not in actionable_scope_ids:
            raise serializers.ValidationError(
                {"scope_node": "You do not have permission to create or edit expenses at this scope."}
            )

        org = None
        if scope_node:
            org = scope_node.org
        elif self.instance:
            org = self.instance.org

        if budget and org and budget.org_id != org.id:
            raise serializers.ValidationError(
                {"budget": "Selected budget does not belong to this organization."}
            )
        if category and org and category.org_id != org.id:
            raise serializers.ValidationError(
                {"category": "Selected category does not belong to this organization."}
            )
        if subcategory and category and subcategory.category_id != category.id:
            raise serializers.ValidationError(
                {"subcategory": "Selected subcategory does not belong to the selected category."}
            )
        if budget_line:
            if budget and budget_line.budget_id != budget.id:
                raise serializers.ValidationError(
                    {"budget_line": "Selected budget line does not belong to the selected budget."}
                )
            if category and budget_line.category_id != category.id:
                raise serializers.ValidationError(
                    {"budget_line": "Selected budget line does not belong to the selected category."}
                )
            if subcategory and budget_line.subcategory_id != subcategory.id:
                raise serializers.ValidationError(
                    {"budget_line": "Selected budget line does not belong to the selected subcategory."}
                )
        return attrs


class SubmitExpenseSerializer(serializers.Serializer):
    """No fields needed — submit is a state transition."""
    pass


class SettleExpenseSerializer(serializers.Serializer):
    """No fields needed — settle is a state transition."""
    pass


class CancelExpenseSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, max_length=500)
