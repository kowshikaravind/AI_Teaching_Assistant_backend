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
    ClassChatView,
    SubjectListCreateView,
    SubjectDeleteView,
    StudentLoginView,
    StudentChangePasswordView,
    UpcomingTestListCreateView,
    UpcomingTestRetrieveUpdateDestroyView,
    NotificationListView,
    NotificationMarkReadView,
    NotificationMarkAllReadView,
)

urlpatterns = [
    # ── Students ──────────────────────────────────────────────────
    path('students/', StudentListCreateView.as_view()),
    path('students/<int:pk>/', StudentRetriveDestroyView.as_view()),

    # ── Test Marks ────────────────────────────────────────────────
    path('testmarks/', TestMarkListCreateView.as_view()),
    path('testmarks/<int:pk>/', TestMarkRetrieveDestroyView.as_view()),

    # ── Attendance ────────────────────────────────────────────────
    path('attendance/save/', AttendanceBulkSaveView.as_view()),
    path('attendance/', AttendanceByDateView.as_view()),
    path('students/<int:pk>/attendance-summary/', AttendanceSummaryView.as_view()),

    # ── AI Chat ───────────────────────────────────────────────────
    path('students/<int:pk>/chat/', StudentChatView.as_view()),
    path('class-chat/', ClassChatView.as_view()),

    # ── Subjects ──────────────────────────────────────────────────
    path('subjects/', SubjectListCreateView.as_view()),
    path('subjects/<int:pk>/', SubjectDeleteView.as_view()),

    # ── Upcoming Tests ────────────────────────────────────────────
    path('upcoming-tests/', UpcomingTestListCreateView.as_view()),
    path('upcoming-tests/<int:pk>/', UpcomingTestRetrieveUpdateDestroyView.as_view()),

    # ── Notifications ─────────────────────────────────────────────
    path('notifications/', NotificationListView.as_view()),
    path('notifications/<int:pk>/read/', NotificationMarkReadView.as_view()),
    path('notifications/mark-all-read/', NotificationMarkAllReadView.as_view()),

    # ── Student Login ─────────────────────────────────────────────
    path('student-login/', StudentLoginView),   # ← no .as_view() since it's a function view
    path('student-change-password/', StudentChangePasswordView),
]