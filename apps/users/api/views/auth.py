from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.users.api.serializers.auth import (
    EmbedLoginSerializer,
    LoginSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetValidateSerializer,
)
from apps.users.api.serializers.users import UserSerializer


class LoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]
        tokens = serializer.get_tokens(user)
        return Response({"user": UserSerializer(user).data, **tokens})


class EmbedLoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = EmbedLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]
        tokens = serializer.get_tokens(user)
        return Response({"user": UserSerializer(user).data, **tokens})


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data)


class PasswordResetValidateView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, uid, token):
        serializer = PasswordResetValidateSerializer(data={"uid": uid, "token": token})
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]
        return Response({
            "email": user.email,
            "name": user.get_full_name(),
        })


class PasswordResetConfirmView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, uid, token):
        serializer = PasswordResetConfirmSerializer(
            data={
                "uid": uid,
                "token": token,
                "password": request.data.get("password", ""),
            }
        )
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]
        user.set_password(serializer.validated_data["password"])
        if not user.is_active:
            user.is_active = True
            user.save(update_fields=["password", "is_active", "updated_at"])
        else:
            user.save(update_fields=["password", "updated_at"])
        return Response({"detail": "Password has been reset."})
