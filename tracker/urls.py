from django.urls import path
from .views import (
    StudentListCreateView,
    StudentRetriveDestroyView,
    TestMarkListCreateView,
    TestMarkRetrieveDestroyView,
    StudentChatView,
    StudentAITutorView,
    DeepAnswerScriptAnalysisView,
    ValidateAIAnalysisOutputView,
    SubjectListCreateView,
    SubjectDeleteView,
    StudentLoginView,
    StudentChangePasswordView,
    UpcomingTestListCreateView,
    UpcomingTestRetrieveUpdateDestroyView,
    TeacherQuestionsReviewView,
    DraftTeacherQuestionsView,
    PublishTeacherQuestionsView,
    StudentTestDetailsView,
    StudentTestQuestionsView,
    StudentTestReviewView,
    StudentSingleResponseView,
    StudentSubmitTestView,
    StudentTestResultView,
    NotificationListView,
    NotificationMarkReadView,
    NotificationMarkAllReadView,
    BulkMarkEntryView,
    AdminLoginView,
    AdminChangeCredentialsView,
    TeacherRegisterView,
    TeacherLoginView,
    TeacherClassChatView,
    PendingTeacherListView,
    ApprovedTeacherListView,
    ApproveTeacherView,
    RejectTeacherView,
    RevokeTeacherAccessView,
)

urlpatterns = [
    # ── Students ──────────────────────────────────────────────────
    path('students/', StudentListCreateView.as_view()),
    path('students/<int:pk>/', StudentRetriveDestroyView.as_view()),

    # ── Test Marks ────────────────────────────────────────────────
    path('testmarks/', TestMarkListCreateView.as_view()),
    path('testmarks/<int:pk>/', TestMarkRetrieveDestroyView.as_view()),
    path('testmarks/bulk/', BulkMarkEntryView.as_view()),


    # ── AI Chat ───────────────────────────────────────────────────
    path('students/<int:pk>/chat/', StudentChatView.as_view()),
    path('students/<int:pk>/ai-tutor/', StudentAITutorView.as_view()),
    path('answer-script/deep-analysis/', DeepAnswerScriptAnalysisView.as_view()),
    path('answer-script/validate-analysis/', ValidateAIAnalysisOutputView.as_view()),

    # ── Subjects ──────────────────────────────────────────────────
    path('subjects/', SubjectListCreateView.as_view()),
    path('subjects/<int:pk>/', SubjectDeleteView.as_view()),

    # ── Upcoming Tests ────────────────────────────────────────────
    path('upcoming-tests/', UpcomingTestListCreateView.as_view()),
    path('upcoming-tests/<int:pk>/', UpcomingTestRetrieveUpdateDestroyView.as_view()),
    path('upcoming-tests/<int:pk>/teacher-questions-review/', TeacherQuestionsReviewView.as_view()),
    path('upcoming-tests/<int:pk>/draft-questions/', DraftTeacherQuestionsView.as_view()),
    path('upcoming-tests/<int:pk>/publish-questions/', PublishTeacherQuestionsView.as_view()),
    path('upcoming-tests/<int:pk>/details/', StudentTestDetailsView.as_view()),
    path('upcoming-tests/<int:pk>/questions/', StudentTestQuestionsView.as_view()),
    path('upcoming-tests/<int:pk>/review/', StudentTestReviewView.as_view()),
    path('upcoming-tests/<int:pk>/response/', StudentSingleResponseView.as_view()),
    path('upcoming-tests/<int:pk>/submit/', StudentSubmitTestView.as_view()),
    path('upcoming-tests/<int:pk>/results/', StudentTestResultView.as_view()),

    # ── Notifications ─────────────────────────────────────────────
    path('notifications/', NotificationListView.as_view()),
    path('notifications/<int:pk>/read/', NotificationMarkReadView.as_view()),
    path('notifications/mark-all-read/', NotificationMarkAllReadView.as_view()),

    # ── Student Login ─────────────────────────────────────────────
    path('student-login/', StudentLoginView),   # ← no .as_view() since it's a function view
    path('student-change-password/', StudentChangePasswordView),

    # ── Admin / Teacher Access Management ────────────────────────
    path('admin-login/', AdminLoginView),
    path('admin-change-credentials/', AdminChangeCredentialsView),
    path('teacher-register/', TeacherRegisterView),
    path('teacher-login/', TeacherLoginView),
    path('class-chat/', TeacherClassChatView),
    path('admin/teachers/pending/', PendingTeacherListView),
    path('admin/teachers/approved/', ApprovedTeacherListView),
    path('admin/teachers/<int:pk>/approve/', ApproveTeacherView),
    path('admin/teachers/<int:pk>/reject/', RejectTeacherView),
    path('admin/teachers/<int:pk>/revoke/', RevokeTeacherAccessView),
]
