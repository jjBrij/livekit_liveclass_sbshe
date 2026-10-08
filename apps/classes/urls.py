from django.urls import path
from .views import (
    class_collection,
    class_detail,
    class_token,
    class_join,
    class_leave,
    class_participants,
    class_attendance,
    class_messages,
    class_message_delete,
    control_mute,
    control_unmute,
    control_camera_disable,
    control_camera_enable,
    control_remove,
    control_end,
    control_livekit_participants,
    recording_start,
    recording_stop,
    recording_current,
    recording_list,
     class_hands,
    class_hand_accept,
    class_hand_reject,
    recording_playback_url,

)

app_name = "classes"

urlpatterns = [
    path("", class_collection, name="class-collection"),
    path("<int:class_id>/", class_detail, name="class-detail"),
    path("<int:class_id>/token/", class_token, name="class-token"),
    path("<int:class_id>/join/", class_join, name="class-join"),
    path("<int:class_id>/leave/", class_leave, name="class-leave"),
    path("<int:class_id>/participants/", class_participants, name="class-participants"),
    path("<int:class_id>/attendance/", class_attendance, name="class-attendance"),
    path("<int:class_id>/messages/", class_messages, name="class-messages"),
    path("<int:class_id>/messages/<int:message_id>/delete/", class_message_delete, name="class-message-delete"),

    path("<int:class_id>/controls/mute/", control_mute, name="control-mute"),
    path("<int:class_id>/controls/unmute/", control_unmute, name="control-unmute"),
    path("<int:class_id>/controls/camera/disable/", control_camera_disable, name="control-camera-disable"),
    path("<int:class_id>/controls/camera/enable/", control_camera_enable, name="control-camera-enable"),
    path("<int:class_id>/controls/remove/", control_remove, name="control-remove"),
    path("<int:class_id>/controls/end/", control_end, name="control-end"),
    path("<int:class_id>/controls/livekit-participants/", control_livekit_participants, name="control-livekit-participants"),

    path("<int:class_id>/recording/start/", recording_start, name="recording-start"),
    path("<int:class_id>/recording/stop/", recording_stop, name="recording-stop"),
    path("<int:class_id>/recording/", recording_current, name="recording-current"),
    path("<int:class_id>/recordings/", recording_list, name="recording-list"),
    path("<int:class_id>/recordings/<int:recording_id>/playback-url/",recording_playback_url,name="recording-playback-url"),
            
        
        
    
        # Block 11 — raise hand
    path("<int:class_id>/hands/", class_hands, name="class-hands"),
    path("<int:class_id>/hands/<int:user_id>/accept/", class_hand_accept, name="class-hand-accept"),
    path("<int:class_id>/hands/<int:user_id>/reject/", class_hand_reject, name="class-hand-reject"),
]