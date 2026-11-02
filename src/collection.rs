//! Non-recursive image discovery and inexpensive validation.
//!
//! Paths remain native `PathBuf`s, including non-UTF-8 filenames. The state
//! layer preserves their original bytes through its Unix path serde adapters.

use std::collections::BTreeSet;
use std::fs::{self, Metadata};
use std::io::{BufReader, Read};
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};

use image::{ImageFormat, ImageReader, Limits};

#[derive(Clone, Debug, Default)]
pub struct ScanResult {
    pub paths: Vec<PathBuf>,
    pub warnings: Vec<String>,
}

/// Metadata identity used by the caller's validation cache.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Fingerprint {
    pub dev: u64,
    pub ino: u64,
    pub len: u64,
    pub modified_ns: i128,
}

impl Fingerprint {
    fn from_metadata(metadata: &Metadata) -> Self {
        Self {
            dev: metadata.dev(),
            ino: metadata.ino(),
            len: metadata.len(),
            modified_ns: i128::from(metadata.mtime()) * 1_000_000_000
                + i128::from(metadata.mtime_nsec()),
        }
    }
}

fn supported_extension(path: &Path) -> bool {
    path.extension()
        .and_then(|extension| extension.to_str())
        .is_some_and(|extension| {
            ["jpeg", "jpg", "png", "webp"]
                .iter()
                .any(|allowed| extension.eq_ignore_ascii_case(allowed))
        })
}

/// Scan direct children only, following symlinks to regular files. Canonical
/// paths are deduplicated and sorted; one inaccessible entry never aborts a scan.
/// Hard links with distinct canonical paths intentionally remain distinct.
pub fn scan(roots: &[PathBuf]) -> ScanResult {
    let mut paths = BTreeSet::new();
    let mut warnings = Vec::new();
    for root in roots {
        let entries = match fs::read_dir(root) {
            Ok(entries) => entries,
            Err(error) => {
                warnings.push(format!("{}: {error}", root.display()));
                continue;
            }
        };
        for entry in entries {
            let entry = match entry {
                Ok(entry) => entry,
                Err(error) => {
                    warnings.push(format!("{}: {error}", root.display()));
                    continue;
                }
            };
            let path = entry.path();
            if !supported_extension(&path) {
                continue;
            }
            match fs::canonicalize(&path).and_then(|canonical| {
                fs::metadata(&canonical).map(|metadata| (canonical, metadata))
            }) {
                Ok((canonical, metadata)) if metadata.is_file() => {
                    paths.insert(canonical);
                }
                Ok(_) => {}
                Err(error) => warnings.push(format!("{}: {error}", path.display())),
            }
        }
    }
    warnings.sort();
    ScanResult {
        paths: paths.into_iter().collect(),
        warnings,
    }
}

/// Follow symlinks and return the target's identity. Non-regular files are rejected.
pub fn fingerprint(path: &Path) -> Result<Fingerprint, String> {
    let metadata = fs::metadata(path).map_err(|error| format!("{}: {error}", path.display()))?;
    if !metadata.is_file() {
        return Err(format!("{}: not a regular file", path.display()));
    }
    Ok(Fingerprint::from_metadata(&metadata))
}

/// Inspect at most 1 MiB of header data, with a 64 MiB decoder allocation limit.
/// Dimensions must be nonzero, at most 32,768 per side and 100 million pixels.
/// This deliberately does not decode pixels: corrupt/truncated pixel data may
/// still pass, and valid files with unusually large headers may be rejected.
/// The returned fingerprint belongs to the opened file, not a subsequent lookup.
pub fn validate(path: &Path) -> Result<Fingerprint, String> {
    const HEADER_BYTES: u64 = 1024 * 1024;
    let check = || -> Result<Fingerprint, String> {
        // Check before opening so a FIFO/device is not opened in the usual case.
        // O_NONBLOCK also prevents a concurrent replacement with a FIFO hanging.
        use std::os::unix::fs::OpenOptionsExt;
        let _ = fingerprint(path)?;
        let file = fs::OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NONBLOCK)
            .open(path)
            .map_err(|error| error.to_string())?;
        let metadata = file.metadata().map_err(|error| error.to_string())?;
        if !metadata.is_file() {
            return Err("not a regular file".into());
        }
        let before = Fingerprint::from_metadata(&metadata);
        let mut reader = ImageReader::new(BufReader::new((&file).take(HEADER_BYTES)))
            .with_guessed_format()
            .map_err(|error| error.to_string())?;
        if !matches!(
            reader.format(),
            Some(ImageFormat::Jpeg | ImageFormat::Png | ImageFormat::WebP)
        ) {
            return Err("unsupported or unrecognized image header".into());
        }
        let mut limits = Limits::default();
        limits.max_image_width = Some(32_768);
        limits.max_image_height = Some(32_768);
        limits.max_alloc = Some(64 * 1024 * 1024);
        reader.limits(limits);
        let (width, height) = reader
            .into_dimensions()
            .map_err(|error| error.to_string())?;
        if width == 0
            || height == 0
            || width > 32_768
            || height > 32_768
            || u64::from(width) * u64::from(height) > 100_000_000
        {
            return Err(format!("image dimensions {width}x{height} exceed limits"));
        }
        let after =
            Fingerprint::from_metadata(&file.metadata().map_err(|error| error.to_string())?);
        if before != after {
            return Err("file changed during validation".into());
        }
        Ok(after)
    };
    check().map_err(|error| format!("{}: {error}", path.display()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    struct TempDir(PathBuf);
    impl TempDir {
        fn new() -> Self {
            static NEXT: AtomicU64 = AtomicU64::new(0);
            loop {
                let path = std::env::temp_dir().join(format!(
                    "swiftwall-collection-{}-{}",
                    std::process::id(),
                    NEXT.fetch_add(1, Ordering::Relaxed)
                ));
                match fs::create_dir(&path) {
                    Ok(()) => return Self(path),
                    Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
                    Err(error) => panic!("create temporary directory: {error}"),
                }
            }
        }
    }
    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn scan_is_shallow_sorted_case_insensitive_and_canonical() {
        let dir = TempDir::new();
        let a = dir.0.join("a.JpG");
        let b = dir.0.join("b.PNG");
        fs::write(&a, b"a").unwrap();
        fs::write(&b, b"b").unwrap();
        fs::write(dir.0.join("c.JPEG"), b"c").unwrap();
        fs::write(dir.0.join("d.WeBp"), b"d").unwrap();
        fs::write(dir.0.join("ignored.gif"), b"ignored").unwrap();
        fs::create_dir(dir.0.join("nested.jpg")).unwrap();
        fs::write(dir.0.join("nested.jpg/hidden.png"), b"hidden").unwrap();
        std::os::unix::fs::symlink(&a, dir.0.join("alias.jpg")).unwrap();
        std::os::unix::fs::symlink(dir.0.join("absent"), dir.0.join("broken.png")).unwrap();
        let result = scan(&[dir.0.clone(), dir.0.join("."), dir.0.join("missing")]);
        let expected: Vec<_> = [a, b, dir.0.join("c.JPEG"), dir.0.join("d.WeBp")]
            .iter()
            .map(|p| fs::canonicalize(p).unwrap())
            .collect();
        assert_eq!(result.paths, expected);
        assert!(result.warnings.iter().any(|w| w.contains("missing")));
        assert!(result.warnings.iter().any(|w| w.contains("broken.png")));
    }

    #[test]
    fn validates_headers_and_rejects_missing_corrupt_and_nonregular() {
        let dir = TempDir::new();
        assert!(validate(&dir.0).is_err());
        assert!(validate(&dir.0.join("missing.png")).is_err());
        for format in [ImageFormat::Png, ImageFormat::Jpeg, ImageFormat::WebP] {
            let path = dir.0.join(format!("image.{}", format.extensions_str()[0]));
            image::RgbImage::new(3, 2)
                .save_with_format(&path, format)
                .unwrap();
            assert_eq!(validate(&path).unwrap(), fingerprint(&path).unwrap());
            fs::write(&path, b"not an image").unwrap();
            assert!(validate(&path).is_err());
            fs::write(&path, b"\x89PNG\r\n\x1a\n").unwrap();
            assert!(validate(&path).is_err());
        }
    }

    #[test]
    fn rejects_oversized_dimensions_and_accepts_image_symlinks() {
        let dir = TempDir::new();
        let path = dir.0.join("image.png");
        image::RgbImage::new(2, 2).save(&path).unwrap();
        let alias = dir.0.join("alias.png");
        std::os::unix::fs::symlink(&path, &alias).unwrap();
        assert_eq!(validate(&alias).unwrap(), fingerprint(&path).unwrap());

        // Modify IHDR and its CRC without allocating a giant pixel buffer.
        let mut bytes = fs::read(&path).unwrap();
        bytes[16..20].copy_from_slice(&40_000u32.to_be_bytes());
        let mut crc = !0u32;
        for byte in &bytes[12..29] {
            crc ^= u32::from(*byte);
            for _ in 0..8 {
                crc = (crc >> 1) ^ (0xedb8_8320u32 & 0u32.wrapping_sub(crc & 1));
            }
        }
        bytes[29..33].copy_from_slice(&(!crc).to_be_bytes());
        fs::write(&path, bytes).unwrap();
        assert!(validate(&path).is_err());
    }

    #[test]
    fn fingerprint_tracks_identity_and_size_and_follows_links() {
        let dir = TempDir::new();
        let path = dir.0.join("image.png");
        let alias = dir.0.join("alias.png");
        fs::write(&path, b"first").unwrap();
        std::os::unix::fs::symlink(&path, &alias).unwrap();
        let first = fingerprint(&path).unwrap();
        assert_eq!(first, fingerprint(&alias).unwrap());
        fs::write(&path, b"a longer replacement").unwrap();
        assert_ne!(first, fingerprint(&path).unwrap());
        assert!(fingerprint(&dir.0).is_err());
    }
}
