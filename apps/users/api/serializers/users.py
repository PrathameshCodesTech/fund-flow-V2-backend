from django.contrib.auth import get_user_model
from django.db.models import Q
from rest_framework import serializers

User = get_user_model()


class UserSerializer(serializers.ModelSerializer):
    assigned_roles = serializers.SerializerMethodField()
    capabilities = serializers.SerializerMethodField()
    is_vendor_portal_user = serializers.SerializerMethodField()
    vendor_id = serializers.SerializerMethodField()
    vendor_name = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "id", "email", "first_name", "last_name", "employee_id",
            "is_active", "is_staff", "is_superuser", "date_joined",
            "assigned_roles", "capabilities",
            "is_vendor_portal_user", "vendor_id", "vendor_name",
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

    class Meta:
        model = User
        fields = ("id", "email", "first_name", "last_name", "employee_id", "is_active", "password")
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
        return data

    def create(self, validated_data):
        password = validated_data.pop("password", None)
        if not password:
            password = User.objects.make_random_password()
        user = User.objects.create_user(password=password, **validated_data)
        return user


class UserListSerializer(serializers.ModelSerializer):
    """Serializer for user list endpoint — excludes sensitive fields."""
    assigned_roles = serializers.SerializerMethodField()
    capabilities = serializers.SerializerMethodField()
    is_vendor_portal_user = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "id", "email", "first_name", "last_name", "employee_id",
            "is_active", "is_staff", "date_joined",
            "assigned_roles", "capabilities", "is_vendor_portal_user",
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
