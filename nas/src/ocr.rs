use anyhow::{ensure, Context, Result};
use image::{
    imageops::{resize, FilterType},
    DynamicImage, ImageReader, Limits,
};
use ort::{session::Session, value::Tensor};
use std::{env, io::Cursor};

#[derive(Default)]
pub struct Ocr {
    session: Option<Session>,
}

impl Ocr {
    fn session(&mut self) -> Result<&mut Session> {
        if self.session.is_none() {
            let library = env::var("COURT_OCR_LIBRARY")
                .unwrap_or_else(|_| "/opt/court-ocr/libonnxruntime.so".to_owned());
            let model = env::var("COURT_OCR_MODEL")
                .unwrap_or_else(|_| "/opt/court-ocr/common_old.onnx".to_owned());
            ort::init_from(library)?.with_telemetry(false).commit();
            self.session = Some(
                Session::builder()
                    .map_err(|err| anyhow::anyhow!("{err}"))?
                    .with_intra_threads(1)
                    .map_err(|err| anyhow::anyhow!("{err}"))?
                    .commit_from_file(model)?,
            );
        }
        Ok(self.session.as_mut().expect("session was initialized"))
    }

    pub fn infer(&mut self, png: &[u8]) -> Result<String> {
        let mut reader = ImageReader::new(Cursor::new(png)).with_guessed_format()?;
        let mut limits = Limits::default();
        limits.max_image_width = Some(4096);
        limits.max_image_height = Some(4096);
        reader.limits(limits);
        let mut rgba = reader.decode()?.to_rgba8();
        let height = rgba.height();
        ensure!(height > 0, "empty OCR image");
        for px in rgba.pixels_mut() {
            let alpha = u32::from(px[3]);
            for ch in &mut px.0[..3] {
                *ch = ((u32::from(*ch) * alpha + 255 * (255 - alpha) + 127) / 255) as u8;
            }
            px[3] = 255;
        }
        let width = (u64::from(rgba.width()) * 64 / u64::from(height)).clamp(1, 4096) as u32;
        let gray =
            DynamicImage::ImageRgba8(resize(&rgba, width, 64, FilterType::Lanczos3)).into_luma8();
        let pixels = gray
            .into_vec()
            .into_iter()
            .map(|pixel| f32::from(pixel) / 255.0)
            .collect::<Vec<_>>();
        let session = self.session()?;
        let tensor = Tensor::from_array(([1, 1, 64, width as usize], pixels))?;
        let output = session.run(ort::inputs![tensor])?;
        let (shape, scores) = output[0].try_extract_tensor::<f32>()?;
        ensure!(shape.last() == Some(&8210), "unexpected OCR output shape");
        let mut result = String::new();
        let mut previous = None;
        for scores in scores.chunks_exact(8210) {
            let index = scores
                .iter()
                .enumerate()
                .reduce(|best, x| if x.1 > best.1 { x } else { best })
                .context("empty OCR prediction")?
                .0;
            if previous != Some(index) {
                let character = match index {
                    6749 => '0',
                    4410 => '1',
                    78 => '2',
                    7721 => '3',
                    5806 => '4',
                    6977 => '5',
                    5961 => '6',
                    409 => '7',
                    6979 => '8',
                    2879 => '9',
                    _ => {
                        previous = Some(index);
                        continue;
                    }
                };
                result.push(character);
            }
            previous = Some(index);
        }
        Ok(result)
    }

    pub fn recognize(&mut self, png: &[u8]) -> Result<String> {
        let result = self
            .infer(png)
            .context("보안문자를 자동 인식하지 못했습니다.")?;
        ensure!(result.len() == 6, "보안문자 6자리를 인식하지 못했습니다.");
        Ok(result)
    }
}
