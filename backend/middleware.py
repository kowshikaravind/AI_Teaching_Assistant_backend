"""
Custom middleware to set browser permissions policy headers.
"""


class PermissionsPolicyMiddleware:
    """
    Middleware to set Permissions-Policy header enabling unload event.
    This fixes the "unload is not allowed in this document" violation
    in Django admin's RelatedObjectLookups.js
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        # Remove restrictive Permissions-Policy for admin to allow unload event
        # Setting to empty/wildcard ensures unload event works in Django admin
        response["Permissions-Policy"] = "unload=*"
        return response
