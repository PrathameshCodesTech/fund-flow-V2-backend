from django.contrib.auth import get_user_model
from django.db.models import Q
from rest_framework import serializers

User = get_user_model()


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("id", "email", "first_name", "last_name", "employee_id", "is_active", "date_joined")
        read_only_fields = ("id", "date_joined")


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
