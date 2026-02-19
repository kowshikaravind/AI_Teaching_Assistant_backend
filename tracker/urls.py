from django.urls import path
from .views import StudentListCreateView  # <--- Removed 'analyze_student_view'

urlpatterns = [
    # Only keeping the Student List/Create path
    path('students/', StudentListCreateView.as_view(), name='student-list-create'),
]