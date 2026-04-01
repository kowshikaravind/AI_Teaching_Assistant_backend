from rest_framework_simplejwt.tokens import RefreshToken

def get_tokens_for_user(user, role):
    """
    Manually generate tokens for custom user models (Student, TeacherCredential, AdminCredential).
    Since we are not using the default Django auth_user, we pass the role in the token.
    """
    refresh = RefreshToken()
    
    # Set custom claims
    refresh['user_id'] = user.id
    refresh['role'] = role
    
    if role == 'teacher':
        refresh['name'] = getattr(user, 'teacher_name', getattr(user, 'username', ''))
        refresh['assigned_class'] = getattr(user, 'assigned_class', '')
        refresh['department'] = getattr(user, 'department', '')
    elif role == 'student':
        refresh['name'] = user.name
        refresh['class_name'] = user.class_name
        refresh['roll_number'] = user.roll_number
    elif role == 'admin':
        refresh['name'] = user.username

    return {
        'refresh': str(refresh),
        'access': str(refresh.access_token),
    }
