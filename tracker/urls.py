from django.urls import path
from .views import (
    StudentListCreateView,
    StudentRetriveDestroyView,
    TestMarkListCreateView,
    TestMarkRetrieveDestroyView,
    StudentChatView,
)

urlpatterns = [
    path('students/', StudentListCreateView.as_view()),
    path('students/<int:pk>/', StudentRetriveDestroyView.as_view()),

    # Chat endpoint — replaces old ai-insights
    path('students/<int:pk>/chat/', StudentChatView.as_view()),

    path('testmarks/', TestMarkListCreateView.as_view()),
    path('testmarks/<int:pk>/', TestMarkRetrieveDestroyView.as_view()),
]