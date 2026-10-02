
from rest_framework import authentication
from rest_framework import exceptions
from .auth_client import (
    AuthUser,
    AuthServerError,
    validate_token_with_auth_server,
)
class AuthServerAuthentication(authentication.BaseAuthentication):
    keyword = "Bearer"
    def authenticate(self, request):
        header = authentication.get_authorization_header(request).split()

        if not header or header[0].lower() != self.keyword.lower().encode():
            return None  # No Bearer token -> let other auth classes (if any) try

        if len(header) == 1:
            raise exceptions.AuthenticationFailed(
                "Invalid Authorization header. Token not found."
            )
        if len(header) > 2:
            raise exceptions.AuthenticationFailed(
                "Invalid Authorization header. Token contains spaces."
            )

        try:
            token = header[1].decode()
        except UnicodeError:
            raise exceptions.AuthenticationFailed(
                "Invalid Authorization header. Token is not decodable."
            )

        try:
            user = validate_token_with_auth_server(token)
        except AuthServerError:
            # 503 is more accurate than 401 here: our dependency is down,
            # not the user's credentials.
            raise exceptions.AuthenticationFailed(
                "Authentication service unavailable. Try again later."
            )

        if user is None:
            raise exceptions.AuthenticationFailed("Invalid or expired token.")

        return (user, token)

    def authenticate_header(self, request):
        # Tells DRF to return "WWW-Authenticate: Bearer" on 401 responses.
        return self.keyword