from django.urls import path
from .views import StudentListCreateView, TestMarkListCreateView, TestMarkRetrieveDestroyView, StudentRetriveDestroyView

urlpatterns = [
    path('students/', StudentListCreateView.as_view(), name='student-list-create'),
    path('students/<int:pk>/', StudentRetriveDestroyView.as_view(), name='student-retrieve-destroy'),
    path('testmarks/', TestMarkListCreateView.as_view(), name='testmark-list-create'),
    path('testmarks/<int:pk>/', TestMarkRetrieveDestroyView.as_view(), name='testmark-detail'),
    
]