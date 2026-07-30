from django.contrib.auth import authenticate
from rest_framework import serializers

from .models import User


class UserSerializer(serializers.ModelSerializer):
    # `is_creator` is now a model property (derived from plan + expiry).
    is_creator = serializers.BooleanField(read_only=True)
    # Report the *effective* plan so clients never have to reimplement expiry
    # logic: an expired creator plan reads as "free" here.
    plan = serializers.SerializerMethodField()
    usage = serializers.SerializerMethodField()
    # §H (Handoff #11): venue branding. Writable only through the profile
    # PATCH, where ProfileView gates writes on the creator plan + storage
    # quota BEFORE this serializer runs (rule 4 — the view is the gate);
    # create() below drops them so register can't sneak branding in.
    # Clearing the logo is the dedicated `brand_logo_clear: true` flag
    # (documented choice — multipart can't reliably send "empty file").
    brand_logo_clear = serializers.BooleanField(write_only=True, required=False)

    class Meta:
        model = User
        fields = (
            "id", "email", "password", "display_name", "date_of_birth",
            "is_creator", "is_staff", "plan", "plan_expires_at", "usage",
            "brand_name", "brand_logo", "brand_logo_clear",
        )
        read_only_fields = ("id", "plan_expires_at", "is_staff")
        extra_kwargs = {"password": {"write_only": True, "min_length": 8}}

    def get_plan(self, obj):
        return obj.effective_plan

    def get_usage(self, obj):
        from . import quotas

        return quotas.usage(obj)

    def create(self, validated_data):
        # §H: branding is never settable at register (the write gate lives on
        # the profile PATCH; a fresh account is free-plan anyway).
        for field in ("brand_name", "brand_logo", "brand_logo_clear"):
            validated_data.pop(field, None)
        return User.objects.create_user(**validated_data)

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        # §H: the clear flag translates to "no logo"; the full save() below
        # recounts brand_logo_bytes to 0, freeing the storage quota.
        if validated_data.pop("brand_logo_clear", False):
            validated_data["brand_logo"] = None
        user = super().update(instance, validated_data)
        if password:
            user.set_password(password)
            user.save(update_fields=["password"])
        return user


class AuthSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(style={"input_type": "password"}, trim_whitespace=False)

    def validate(self, attrs):
        user = authenticate(
            request=self.context.get("request"),
            email=attrs["email"],
            password=attrs["password"],
        )
        if not user:
            raise serializers.ValidationError("Invalid email or password.", code="authorization")
        attrs["user"] = user
        return attrs
