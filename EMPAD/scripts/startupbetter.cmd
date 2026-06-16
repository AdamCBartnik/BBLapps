grabinit
grab_display_on
padcom dac dac_t1_vth.conf
camwait 1
padcom load_dac
padcom pcr null
frametriggersource 0
padcom mult 2779
padcom mildisp 2 1 0
camwait 1
mstatus
padcom tiledebounceflag 1
camwait 1
frametriggersource 1

