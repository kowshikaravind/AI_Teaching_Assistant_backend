from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import AuthenticationFailed
from .models import Student, TeacherCredential, AdminCredential

class CustomJWTAuthentication(JWTAuthentication):
    def get_user(self, validated_token):
        user_id = validated_token.get('user_id')
        role = validated_token.get('role')

        if not user_id or not role:
            raise AuthenticationFailed('Token contained no recognizable user identification')

        if role == 'teacher':
            try:
                user = TeacherCredential.objects.get(id=user_id)
            except TeacherCredential.DoesNotExist:
                raise AuthenticationFailed('Teacher not found', code='user_not_found')
        elif role == 'student':
            try:
                user = Student.objects.get(id=user_id)
            except Student.DoesNotExist:
                raise AuthenticationFailed('Student not found', code='user_not_found')
        elif role == 'admin':
            try:
                user = AdminCredential.objects.get(id=user_id)
            except AdminCredential.DoesNotExist:
                raise AuthenticationFailed('Admin not found', code='user_not_found')
        else:
            raise AuthenticationFailed('Invalid role in token', code='user_not_found')

        # Attach the role to the user object dynamically so views can check it
        user.role = role
        # Attach teacher-specific fields needed by view helpers
        if role == 'teacher':
            # status is required by _get_teacher_from_request() in views.py
            # assigned_class is required by _get_teacher_class() in views.py
            if not hasattr(user, 'status'):
                user.status = getattr(user, 'status', 'approved')
            if not hasattr(user, 'assigned_class'):
                user.assigned_class = getattr(user, 'assigned_class', '')
        # SimpleJWT and DRF expect user.is_active and user.is_authenticated
        user.is_active = True
        user.is_authenticated = True
        return user
