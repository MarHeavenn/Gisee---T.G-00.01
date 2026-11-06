idf_component_register(SRCS "main.c" "gisee_audio.c" "gisee_net.c"
                       INCLUDE_DIRS "."
                       REQUIRES driver esp_http_client esp_wifi esp_event
                                esp_netif nvs_flash esp_codec_dev)
