
from rest_framework import permissions
class IsClassOwner(permissions.BasePermission):
    message = "You do not own this class."

    def has_object_permission(self, request, view, obj):
        user = request.user
        return bool(user) and getattr(user, "user_id", None) == obj.teacher_id

class IsParticipantOrOwner(permissions.BasePermission):
    """
    Object-level permission for endpoints that accept a LiveClass.

    Returns True if:
        - the caller is the class owner (teacher), or
        - the caller is an active participant of the class.

    Used on top of the default authentication requirement.
    """

    message = "You do not have access to this class."

    def has_object_permission(self, request, view, obj):
        user = request.user
        if not user:
            return False
        if getattr(user, "user_id", None) == obj.teacher_id:
            return True
        # Import here to avoid a circular import at module load time.
        from .models import ClassParticipant, ParticipantStatus
        return ClassParticipant.objects.filter(
            live_class=obj,
            user_id=user.user_id,
            status=ParticipantStatus.ACTIVE,
        ).exists()    
    