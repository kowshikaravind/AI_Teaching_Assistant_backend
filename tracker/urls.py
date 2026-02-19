from django.urls import path
from .views import StudentListCreateView, StudentRetrieveView, TestMarkListCreateView, TestMarkRetrieveDestroyView

urlpatterns = [
    path('students/', StudentListCreateView.as_view(), name='student-list-create'),
    path('students/<int:pk>/', StudentRetrieveView.as_view(), name='student-detail'),
    path('testmarks/', TestMarkListCreateView.as_view(), name='testmark-list-create'),
    path('testmarks/<int:pk>/', TestMarkRetrieveDestroyView.as_view(), name='testmark-detail'),
]