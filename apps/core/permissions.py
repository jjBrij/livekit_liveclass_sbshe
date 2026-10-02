

from rest_framework import permissions
from .auth_client import AuthUser
class IsAuthenticatedViaAuthServer(permissions.BasePermission):
    message = "Authentication required."
    def has_permission(self, request, view):
        return isinstance(request.user, AuthUser)
class IsTeacher(permissions.BasePermission):
    message = "Teacher role required."
    def has_permission(self, request, view):
        user = request.user
        return isinstance(user, AuthUser) and user.is_teacher


class IsStudent(permissions.BasePermission):
    message = "Student role required."
    def has_permission(self, request, view):
        user = request.user
        return isinstance(user, AuthUser) and user.is_student