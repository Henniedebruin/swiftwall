//! Lossless Unix path persistence: UTF-8 strings stay compatible with old JSON,
//! while non-UTF-8 paths use an object containing their exact native bytes.

use std::ffi::OsString;
use std::os::unix::ffi::{OsStrExt, OsStringExt};
use std::path::{Path, PathBuf};

use serde::{Deserialize, Deserializer, Serialize, Serializer};

#[derive(Serialize, Deserialize)]
#[serde(untagged)]
enum PathRepr {
    Utf8(String),
    UnixBytes { unix_bytes: Vec<u8> },
}

impl PathRepr {
    fn from_path(path: &Path) -> Result<Self, &'static str> {
        let bytes = path.as_os_str().as_bytes();
        reject_nul(bytes)?;
        Ok(match path.to_str() {
            Some(text) => Self::Utf8(text.to_owned()),
            None => Self::UnixBytes {
                unix_bytes: bytes.to_vec(),
            },
        })
    }

    fn into_path(self) -> Result<PathBuf, &'static str> {
        let bytes = match self {
            Self::Utf8(text) => text.into_bytes(),
            Self::UnixBytes { unix_bytes } => unix_bytes,
        };
        reject_nul(&bytes)?;
        Ok(OsString::from_vec(bytes).into())
    }
}

fn reject_nul(bytes: &[u8]) -> Result<(), &'static str> {
    if bytes.contains(&0) {
        Err("paths must not contain NUL bytes")
    } else {
        Ok(())
    }
}

pub mod vec {
    use super::*;
    use serde::ser::SerializeSeq;

    pub fn serialize<S: Serializer>(paths: &[PathBuf], serializer: S) -> Result<S::Ok, S::Error> {
        let mut sequence = serializer.serialize_seq(Some(paths.len()))?;
        for path in paths {
            let repr = PathRepr::from_path(path).map_err(serde::ser::Error::custom)?;
            sequence.serialize_element(&repr)?;
        }
        sequence.end()
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(
        deserializer: D,
    ) -> Result<Vec<PathBuf>, D::Error> {
        Vec::<PathRepr>::deserialize(deserializer)?
            .into_iter()
            .map(|repr| repr.into_path().map_err(serde::de::Error::custom))
            .collect()
    }
}

pub mod option {
    use super::*;

    pub fn serialize<S: Serializer>(
        path: &Option<PathBuf>,
        serializer: S,
    ) -> Result<S::Ok, S::Error> {
        path.as_deref()
            .map(PathRepr::from_path)
            .transpose()
            .map_err(serde::ser::Error::custom)?
            .serialize(serializer)
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(
        deserializer: D,
    ) -> Result<Option<PathBuf>, D::Error> {
        Option::<PathRepr>::deserialize(deserializer)?
            .map(PathRepr::into_path)
            .transpose()
            .map_err(serde::de::Error::custom)
    }
}
