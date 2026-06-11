# camera_ioc

Generic EPICS camera-IOC infrastructure for BBL: a single PV contract
modeled on standard **areaDetector** (verified against ADCore's ADDriver
records and NDArrayBase.template), plus the standalone tools that serve it.
Any standard areaDetector client (beamview, Phoebus, EDM, caget) works
against any IOC built on this.

## Files

| File | What it is |
|---|---|
| `ad_ioc_base.py` | The contract: `ADCameraIOCBase` (caproto PVGroup serving `cam1:`/`image1:` standard records + extensions) and the `CameraDriver` ABC. Concrete IOCs = a driver plugged into `build_ioc_class()`. |
| `vpcam_extensions.py` | Canonical metadata for the VPCam extension PV surface (LED, AE, flips, system info, IMX708 extras). Consumers bind their own accessors. |
| `gateway_ioc.py` | Standalone CA gateway: `python gateway_ioc.py VPCAM:03` relays a source IOC at `VPCAM:03:GW`. Event-driven (camonitors the source's frame counter), self-healing (restarts an idle source while relaying), never stops the source. |
| `gige_ioc.py` | Standalone GigE Vision camera IOC via Harvester: `python gige_ioc.py 192.168.128.2 B29CAM1`. Run on a machine sharing the camera subnet; needs `pip install harvesters` + a vendor GenTL `.cti`. |
| `mock_ioc.py` | Standalone mock camera (drifting Gaussian, no hardware): `python mock_ioc.py MOCKCAM:01`. Also provides `MockDriver` for other consumers. |
| `test_ad_contract.py` | Contract smoke test: fake driver, real CA round-trip. |
| `test_relay_chain.py` | Gateway end-to-end test: mock source (booted idle) ← gateway ← client, including the starvation self-heal. |

## The contract in one paragraph

`cam1:` serves the genuine areaDetector driver records — `Acquire(_RBV)`,
`ImageMode`, `NumImages`, `AcquireTime(_RBV)` (seconds), `AcquirePeriod`,
`Gain(_RBV)`, ROI as `MinX/MinY/SizeX/SizeY(_RBV)` (immediate, clamped),
`MaxSizeX/Y_RBV`, `DataType`, `ColorMode`, `ArrayCounter(_RBV)`,
`ArrayRate_RBV`, `Manufacturer/Model_RBV` — plus marked extensions
(`BitsPerPixel_RBV`, `CalibX/Y` in µm/px). `image1:` serves the NDStdArrays
equivalent: `ArrayData` (fixed NELM = max frame, active w×h prefix, read
with a counted get), `ArraySize0/1_RBV`, `TimeStamp_RBV`, `UniqueId_RBV`,
and **`ArrayCounter_RBV` — the new-frame monitor PV**, written last per
frame so all metadata is consistent when it fires. Full reference:
`vpcam/docs/pvs.md` in the vpcam repo.

## Consumers

- **vpcam** (separate repo): the Raspberry Pi camera product. Its
  `vpcam_launcher.py` imports `ad_ioc_base`/`vpcam_extensions`/`mock_ioc`
  from here — set `CAMERA_IOC_PATH`, or check this repo out next to the
  vpcam repo or in the home directory.
- **beamview** (this repo): `EPICSAreaDetectorCamera` speaks this contract;
  `beamview/test_ad_backend.py` tests it against `mock_ioc.py`.

## Dependencies

`pip install caproto numpy` — plus `harvesters` for `gige_ioc.py` only.
Clients reading image PVs need `EPICS_CA_MAX_ARRAY_BYTES=40000000`.
