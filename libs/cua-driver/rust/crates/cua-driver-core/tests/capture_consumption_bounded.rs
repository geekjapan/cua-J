#[path = "../src/capture_registry.rs"]
mod capture_registry;
use capture_registry::*;
pub use cua_driver_core::image_utils;
use cua_driver_core::image_utils::encode_rgba_to_png;
use std::sync::{
    atomic::{AtomicU64, Ordering},
    Arc,
};
use std::time::Duration;

struct Clock(AtomicU64);
impl MonotonicClock for Clock {
    fn now(&self) -> Duration {
        Duration::from_secs(self.0.load(Ordering::SeqCst))
    }
}

#[test]
fn bounded_capture_consumption() {
    let png = encode_rgba_to_png(&[255; 16], 2, 2).unwrap();
    for trace in 0..10usize.pow(4) {
        let clock = Arc::new(Clock(AtomicU64::new(0)));
        let registry =
            CaptureService::with_clock(CaptureRegistryConfig::default(), clock.clone()).unwrap();
        let owner = registry.binding(Arc::<str>::from("owner"), 1).unwrap();
        let target = CaptureTarget::PrimaryDesktop;
        let id = registry
            .publish(CapturePublication {
                png_bytes: png.clone(),
                target: target.clone(),
                encoded_dimensions: EncodedScreenshotDimensions::new(2, 2).unwrap(),
                native_action_dimensions: NativeActionDimensions::new(2, 2).unwrap(),
                screenshot_to_action: ScreenshotToActionTransform::identity(),
                session_id: Arc::<str>::from("owner"),
                session_generation: 1,
            })
            .unwrap();
        let mut live = true;
        let mut admitted = 0;
        let mut digits = trace;
        for step in 0..4 {
            let op = digits % 10;
            digits /= 10;
            match op {
                0 => {
                    let result = registry.admit_action(request(
                        id,
                        owner.clone(),
                        target.clone(),
                        false,
                        false,
                    ));
                    assert_eq!(result.is_ok(), live, "trace {trace} step {step}");
                    admitted += usize::from(result.is_ok());
                    live = false;
                }
                1..=4 => {
                    let runtime = registry.runtime_generation() + u64::from(op == 1);
                    let session = if op == 2 { "other" } else { "owner" };
                    let generation = if op == 3 { 2 } else { 1 };
                    let other = CaptureBinding::new(runtime, Arc::<str>::from(session), generation)
                        .unwrap();
                    let other_target = if op == 4 {
                        CaptureTarget::Window {
                            pid: 7,
                            window_id: 9,
                        }
                    } else {
                        target.clone()
                    };
                    assert!(
                        registry
                            .admit_action(request(id, other, other_target, false, false))
                            .is_err(),
                        "trace {trace} step {step}"
                    );
                }
                5 => {
                    registry.retire_session(&owner);
                    live = false;
                }
                6 => {
                    clock.0.store(60, Ordering::SeqCst);
                    live = false;
                }
                7 => assert_eq!(
                    registry.read_for_perception(id, &owner).is_ok(),
                    live,
                    "trace {trace} step {step}"
                ),
                8..=9 => assert!(registry
                    .admit_action(request(id, owner.clone(), target.clone(), op == 8, op == 9))
                    .is_err()),
                _ => unreachable!(),
            }
            assert!(admitted <= 1, "trace {trace} admitted {admitted}");
        }
    }
}

fn request(
    id: CaptureId,
    binding: CaptureBinding,
    target: CaptureTarget,
    bad_point: bool,
    bad_frame: bool,
) -> CaptureActionRequest {
    CaptureActionRequest {
        capture_id: id,
        binding,
        target,
        current_native_action_dimensions: NativeActionDimensions::new(
            if bad_frame { 3 } else { 2 },
            2,
        )
        .unwrap(),
        screenshot_x: if bad_point { 2.0 } else { 1.0 },
        screenshot_y: 1.0,
    }
}
