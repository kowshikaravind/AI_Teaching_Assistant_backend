from django.urls import path
from .views import (
    StudentListCreateView,
    StudentRetriveDestroyView,
    TestMarkListCreateView,
    TestMarkRetrieveDestroyView,
    AttendanceBulkSaveView,
    AttendanceSummaryView,
    AttendanceByDateView,
    StudentChatView,
)
from .views import ClassChatView
from .views import SubjectListCreateView, SubjectDeleteView

urlpatterns = [
    # ── Students ──────────────────────────────────────────────────
    path('students/', StudentListCreateView.as_view()),
    path('students/<int:pk>/', StudentRetriveDestroyView.as_view()),

    # ── Test Marks ────────────────────────────────────────────────
    path('testmarks/', TestMarkListCreateView.as_view()),
    path('testmarks/<int:pk>/', TestMarkRetrieveDestroyView.as_view()),

    # ── Attendance ────────────────────────────────────────────────
    path('attendance/save/', AttendanceBulkSaveView.as_view()),        # POST — bulk save
    path('attendance/', AttendanceByDateView.as_view()),               # GET  — load attendance by date
    path('students/<int:pk>/attendance-summary/', AttendanceSummaryView.as_view()),  # GET — per student %

    # ── AI Chat ───────────────────────────────────────────────────
    path('students/<int:pk>/chat/', StudentChatView.as_view()),

    path('class-chat/', ClassChatView.as_view()),
    
    path('subjects/', SubjectListCreateView.as_view()),
    path('subjects/<int:pk>/', SubjectDeleteView.as_view()),
]