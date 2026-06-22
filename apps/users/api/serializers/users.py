from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from rest_framework import serializers

from apps.access.models import Role, UserRoleAssignment
from apps.core.models import ScopeNode

User = get_user_model()


class UserSerializer(serializers.ModelSerializer):
    assigned_roles = serializers.SerializerMethodField()
    capabilities = serializers.SerializerMethodField()
    is_vendor_portal_user = serializers.SerializerMethodField()
    user_type = serializers.SerializerMethodField()
    vendor_id = serializers.SerializerMethodField()
    vendor_name = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "id", "email", "first_name", "last_name", "employee_id",
            "is_active", "is_staff", "is_superuser", "date_joined",
            "assigned_roles", "capabilities",
            "is_vendor_portal_user", "user_type", "vendor_id", "vendor_name",
        )
        read_only_fields = ("id", "date_joined")

    def get_assigned_roles(self, obj):
        from apps.access.models import UserRoleAssignment
        assignments = (
            UserRoleAssignment.objects
            .select_related("role")
            .filter(user=obj, role__is_active=True)
        )
        seen = set()
        result = []
        for a in assignments:
            if a.role.code not in seen:
                seen.add(a.role.code)
                result.append({"code": a.role.code, "name": a.role.name})
        return result

    def get_capabilities(self, obj):
        from apps.access.capabilities import get_user_capabilities
        return get_user_capabilities(obj)

    def get_is_vendor_portal_user(self, obj):
        from apps.vendors.models import UserVendorAssignment
        return UserVendorAssignment.objects.filter(user=obj, is_active=True).exists()

    def get_user_type(self, obj):
        return "vendor" if self.get_is_vendor_portal_user(obj) else "internal"

    def get_vendor_id(self, obj):
        from apps.vendors.models import UserVendorAssignment
        assignment = (
            UserVendorAssignment.objects
            .select_related("vendor")
            .filter(user=obj, is_active=True)
            .first()
        )
        return str(assignment.vendor_id) if assignment else None

    def get_vendor_name(self, obj):
        from apps.vendors.models import UserVendorAssignment
        assignment = (
            UserVendorAssignment.objects
            .select_related("vendor")
            .filter(user=obj, is_active=True)
            .first()
        )
        return assignment.vendor.vendor_name if assignment else None


class UserCreateSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=False, allow_blank=True)
    role = serializers.PrimaryKeyRelatedField(
        queryset=Role.objects.filter(is_active=True),
        write_only=True,
    )
    scope_node = serializers.PrimaryKeyRelatedField(
        queryset=ScopeNode.objects.filter(is_active=True),
        write_only=True,
    )

    class Meta:
        model = User
        fields = (
            "id", "email", "first_name", "last_name", "employee_id",
            "is_active", "password", "role", "scope_node",
        )
        read_only_fields = ("id",)

    def validate_email(self, value):
        return value.lower().strip()

    def validate_employee_id(self, value):
        if value:
            return value.strip()
        return value

    def validate(self, data):
        employee_id = data.get("employee_id")
        if employee_id:
            qs = User.objects.filter(Q(employee_id=employee_id) & Q(is_active=True))
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError({"employee_id": "This employee ID is already in use."})

        role = data["role"]
        scope_node = data["scope_node"]
        if role.org_id != scope_node.org_id:
            raise serializers.ValidationError({
                "role": "The selected role belongs to a different organization than the selected scope.",
            })
        if role.node_type_scope and role.node_type_scope != scope_node.node_type:
            raise serializers.ValidationError({
                "scope_node": (
                    f"The {role.name} role can only be assigned at {role.node_type_scope} scope nodes."
                ),
            })
        return data

    @transaction.atomic
    def create(self, validated_data):
        password = validated_data.pop("password", None)
        role = validated_data.pop("role")
        scope_node = validated_data.pop("scope_node")
        if not password:
            password = User.objects.make_random_password()
        user = User.objects.create_user(password=password, **validated_data)
        UserRoleAssignment.objects.create(user=user, role=role, scope_node=scope_node)
        return user


class UserListSerializer(serializers.ModelSerializer):
    """Serializer for user list endpoint — excludes sensitive fields."""
    assigned_roles = serializers.SerializerMethodField()
    capabilities = serializers.SerializerMethodField()
    is_vendor_portal_user = serializers.SerializerMethodField()
    user_type = serializers.SerializerMethodField()
    vendor_id = serializers.SerializerMethodField()
    vendor_name = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "id", "email", "first_name", "last_name", "employee_id",
            "is_active", "is_staff", "date_joined",
            "assigned_roles", "capabilities", "is_vendor_portal_user",
            "user_type", "vendor_id", "vendor_name",
        )
        read_only_fields = ("id", "date_joined")

    def get_assigned_roles(self, obj):
        from apps.access.models import UserRoleAssignment
        assignments = (
            UserRoleAssignment.objects
            .select_related("role")
            .filter(user=obj, role__is_active=True)
        )
        seen = set()
        result = []
        for a in assignments:
            if a.role.code not in seen:
                seen.add(a.role.code)
                result.append({"code": a.role.code, "name": a.role.name})
        return result

    def get_capabilities(self, obj):
        from apps.access.capabilities import get_user_capabilities
        return get_user_capabilities(obj)

    def get_is_vendor_portal_user(self, obj):
        from apps.vendors.models import UserVendorAssignment
        return UserVendorAssignment.objects.filter(user=obj, is_active=True).exists()

    def get_user_type(self, obj):
        return "vendor" if self.get_is_vendor_portal_user(obj) else "internal"

    def get_vendor_id(self, obj):
        from apps.vendors.models import UserVendorAssignment
        assignment = (
            UserVendorAssignment.objects
            .select_related("vendor")
            .filter(user=obj, is_active=True)
            .first()
        )
        return str(assignment.vendor_id) if assignment else None

    def get_vendor_name(self, obj):
        from apps.vendors.models import UserVendorAssignment
        assignment = (
            UserVendorAssignment.objects
            .select_related("vendor")
            .filter(user=obj, is_active=True)
            .first()
        )
        return assignment.vendor.vendor_name if assignment else None


class UserUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("id", "email", "first_name", "last_name", "employee_id", "is_active")
        read_only_fields = ("id", "email")

    def validate_employee_id(self, value):
        if value:
            value = value.strip()
            qs = User.objects.filter(employee_id=value, is_active=True)
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError("This employee ID is already in use.")
        return value

    def validate_email(self, value):
        return value.lower().strip()

    def validate(self, attrs):
        if (
            self.instance
            and self.instance.is_active
            and attrs.get("is_active") is False
        ):
            from apps.workflow.responsibility_services import (
                get_pending_workflow_responsibility_count,
            )

            counts = get_pending_workflow_responsibility_count(self.instance)
            if counts["total"]:
                raise serializers.ValidationError({
                    "is_active": (
                        f"Reassign {counts['total']} pending workflow "
                        "responsibility/responsibilities before deactivating this user."
                    ),
                })
        return attrs


class WorkflowResponsibilityReassignSerializer(serializers.Serializer):
    new_user = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.filter(is_active=True),
    )
    reason = serializers.CharField(allow_blank=False, trim_whitespace=True)
