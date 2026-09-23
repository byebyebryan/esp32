idf_component_register(
    SRCS "main.c"
    INCLUDE_DIRS ".")

# Point LVGL's perf monitor at our own idle reader. Its default
# (lv_os_get_idle_percent) relies on FreeRTOS trace hooks that are not wired up
# here and matches the task name "IDLE", while ESP-IDF names the idle tasks
# "IDLE0"/"IDLE1" - so it always reported 100% CPU.
idf_component_get_property(lvgl_lib lvgl__lvgl COMPONENT_LIB)
if(lvgl_lib)
    target_compile_definitions(${lvgl_lib} PRIVATE LV_SYSMON_GET_IDLE=my_idle_percent)
else()
    message(WARNING "lvgl component not found; perf monitor CPU% will be wrong")
endif()
