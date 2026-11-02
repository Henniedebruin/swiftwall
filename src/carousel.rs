//! A transactional shuffle bag with bounded back/forward history.
//!
//! `next` and `previous` propose a selection; only `commit` records a successful
//! apply. Repeated `next` calls after a failed apply therefore retry the same
//! candidate rather than silently losing it. An already-current final bag entry
//! counts as consumed when alternatives exist, even without a local commit.
//! To exclude a broken image, remove
//! it from the input to `reconcile`. History navigation never consumes the bag.
//! Pending proposals are not serialized. Persisted paths use UTF-8 strings when
//! possible and native Unix byte objects otherwise, without lossy conversion.

use std::path::{Path, PathBuf};

use rand::seq::SliceRandom;
use serde::{Deserialize, Serialize};

const HISTORY_LIMIT: usize = 128;

#[derive(Default, Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct Carousel {
    #[serde(with = "crate::path_serde::vec")]
    paths: Vec<PathBuf>,
    #[serde(with = "crate::path_serde::vec")]
    bag: Vec<PathBuf>,
    #[serde(with = "crate::path_serde::vec")]
    history: Vec<PathBuf>,
    cursor: Option<usize>,
    #[serde(skip)]
    pending: Option<Selection>,
}

#[derive(Clone, Debug)]
struct Selection {
    path: PathBuf,
    history_index: Option<usize>,
}

impl Carousel {
    /// Update membership without resurrecting already consumed bag entries.
    /// Removed paths disappear from both the bag and history. Call after loading
    /// persisted state, too, to normalize membership and bound old history.
    pub fn reconcile(&mut self, paths: &[PathBuf]) {
        let mut paths = paths.to_vec();
        paths.sort();
        paths.dedup();
        self.paths.sort();
        self.paths.dedup();
        let mut seen = std::collections::BTreeSet::new();
        self.bag
            .retain(|path| paths.binary_search(path).is_ok() && seen.insert(path.clone()));
        let mut added = false;
        for path in &paths {
            if self.paths.binary_search(path).is_err() && seen.insert(path.clone()) {
                self.bag.push(path.clone());
                added = true;
            }
        }
        if added {
            self.bag.shuffle(&mut rand::rng());
        }
        let old_cursor = self.cursor;
        let mut retained_before_cursor = 0usize;
        self.history = self
            .history
            .iter()
            .enumerate()
            .filter_map(|(index, path)| {
                if paths.binary_search(path).is_ok() {
                    if old_cursor.is_some_and(|cursor| index <= cursor) {
                        retained_before_cursor += 1;
                    }
                    Some(path.clone())
                } else {
                    None
                }
            })
            .collect();
        self.cursor = retained_before_cursor.checked_sub(1);
        self.trim_history();
        self.paths = paths;
        self.pending = None;
    }

    /// Propose forward history first, otherwise the next shuffle-bag member.
    /// Refill only on exhaustion; with multiple members, never propose `current`.
    /// If it is the final bag entry, count the externally applied current image
    /// as consumed and refill. This observation does not change local history.
    pub fn next(&mut self, current: Option<&Path>) -> Option<PathBuf> {
        self.pending = None;
        let forward = match self.anchor(current) {
            Some(index) => index.checked_add(1),
            None if current.is_none() => Some(0),
            None => None,
        };
        if let Some(index) = forward.filter(|index| *index < self.history.len()) {
            return self.propose(self.history[index].clone(), Some(index));
        }
        if self.paths.is_empty() {
            return None;
        }
        if self.paths.len() > 1 && self.bag.len() == 1 && current == Some(self.bag[0].as_path()) {
            self.bag.clear();
        }
        if self.bag.is_empty() {
            self.bag = self.paths.clone();
            self.bag.shuffle(&mut rand::rng());
        }
        let last = self.bag.len() - 1;
        if current == Some(self.bag[last].as_path())
            && let Some(other) = self
                .bag
                .iter()
                .position(|path| Some(path.as_path()) != current)
        {
            self.bag.swap(last, other);
        }
        self.propose(self.bag[last].clone(), None)
    }

    /// Propose the preceding successful apply, without wrapping. If `current`
    /// is unknown to history, the most recent successful apply is the predecessor.
    pub fn previous(&mut self, current: Option<&Path>) -> Option<PathBuf> {
        self.pending = None;
        let index = match self.anchor(current) {
            Some(index) => index.checked_sub(1),
            None if current.is_some() => self.history.len().checked_sub(1),
            None => None,
        }?;
        self.propose(self.history[index].clone(), Some(index))
    }

    /// Record a successful apply. A matching history proposal moves the cursor;
    /// a fresh selection discards forward history and consumes its bag entry.
    /// Unsolicited commits are accepted only for current collection members.
    pub fn commit(&mut self, path: PathBuf) {
        let pending = self.pending.take();
        if self.paths.binary_search(&path).is_err() {
            return;
        }
        if let Some(index) = pending
            .filter(|selection| selection.path == path)
            .and_then(|selection| selection.history_index)
            .filter(|index| self.history.get(*index) == Some(&path))
        {
            self.cursor = Some(index);
            return;
        }
        self.bag.retain(|candidate| candidate != &path);
        if let Some(cursor) = self.cursor {
            self.history.truncate(cursor.saturating_add(1));
        } else {
            self.history.clear();
        }
        // Applying the same image again (notably a one-member bag) need not
        // fill the history with identical adjacent entries.
        if self.history.last() != Some(&path) {
            self.history.push(path);
        }
        self.cursor = self.history.len().checked_sub(1);
        self.trim_history();
    }

    fn anchor(&self, current: Option<&Path>) -> Option<usize> {
        let cursor = self.cursor.filter(|index| *index < self.history.len());
        match current {
            Some(path) => cursor
                .filter(|index| self.history[*index] == path)
                .or_else(|| self.history.iter().rposition(|entry| entry == path)),
            None => cursor,
        }
    }

    fn propose(&mut self, path: PathBuf, history_index: Option<usize>) -> Option<PathBuf> {
        self.pending = Some(Selection {
            path: path.clone(),
            history_index,
        });
        Some(path)
    }

    fn trim_history(&mut self) {
        let excess = self.history.len().saturating_sub(HISTORY_LIMIT);
        if excess > 0 {
            self.history.drain(..excess);
            self.cursor = self.cursor.and_then(|cursor| cursor.checked_sub(excess));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeSet;

    fn paths(count: usize) -> Vec<PathBuf> {
        (0..count)
            .map(|index| PathBuf::from(format!("/{index}.png")))
            .collect()
    }

    #[test]
    fn non_utf8_paths_round_trip_in_all_persisted_fields() {
        use std::ffi::OsString;
        use std::os::unix::ffi::OsStringExt;

        let bytes = b"/wallpaper-\xff.png".to_vec();
        let path = PathBuf::from(OsString::from_vec(bytes.clone()));
        let carousel = Carousel {
            paths: vec![path.clone(), "/plain.png".into()],
            bag: vec![path.clone()],
            history: vec![path],
            cursor: Some(0),
            pending: None,
        };
        let json = serde_json::to_value(&carousel).unwrap();
        for field in ["paths", "bag", "history"] {
            assert_eq!(json[field][0], serde_json::json!({ "unix_bytes": bytes }));
        }
        assert_eq!(json["paths"][1], "/plain.png");
        let loaded: Carousel = serde_json::from_value(json).unwrap();
        assert_eq!(loaded.paths, carousel.paths);
        assert_eq!(loaded.bag, carousel.bag);
        assert_eq!(loaded.history, carousel.history);
        assert_eq!(loaded.cursor, carousel.cursor);
        assert!(loaded.pending.is_none());
    }

    #[test]
    fn plain_json_paths_and_missing_fields_remain_compatible() {
        let json = serde_json::json!({
            "paths": ["/plain.png", "/café.png"],
            "bag": ["/café.png"],
            "history": ["/plain.png"],
            "cursor": 0
        });
        let carousel: Carousel = serde_json::from_value(json.clone()).unwrap();
        assert_eq!(serde_json::to_value(&carousel).unwrap(), json);
        let empty: Carousel = serde_json::from_str("{}").unwrap();
        assert!(empty.paths.is_empty());
        assert!(empty.bag.is_empty());
        assert!(empty.history.is_empty());
        assert_eq!(empty.cursor, None);
        let partial: Carousel = serde_json::from_str(r#"{"paths":["/plain.png"]}"#).unwrap();
        assert_eq!(partial.paths, vec![PathBuf::from("/plain.png")]);
        assert!(partial.bag.is_empty());
        assert!(partial.history.is_empty());
    }

    #[test]
    fn unique_bags_refill_without_boundary_repeats() {
        let paths = paths(7);
        let mut carousel = Carousel::default();
        carousel.reconcile(&paths);
        let mut current = None;
        for _ in 0..30 {
            let mut seen = BTreeSet::new();
            for _ in &paths {
                let next = carousel.next(current.as_deref()).unwrap();
                assert_ne!(current.as_ref(), Some(&next));
                assert!(seen.insert(next.clone()));
                carousel.commit(next.clone());
                current = Some(next);
            }
            assert_eq!(seen, paths.iter().cloned().collect());
        }
    }

    #[test]
    fn externally_current_final_entry_refills_without_repeating_or_losing_retry() {
        for count in [2, 4] {
            let paths = paths(count);
            let mut carousel = Carousel::default();
            carousel.reconcile(&paths);
            for path in &paths[..count - 1] {
                carousel.commit(path.clone());
            }
            let current = &paths[count - 1];
            assert_eq!(carousel.bag, vec![current.clone()]);
            let history = carousel.history.clone();
            let cursor = carousel.cursor;

            let candidate = carousel.next(Some(current)).unwrap();
            assert_ne!(&candidate, current);
            assert_eq!(carousel.history, history);
            assert_eq!(carousel.cursor, cursor);
            assert_eq!(carousel.bag.len(), count);
            let bag = carousel.bag.clone();
            for _ in 0..3 {
                assert_eq!(carousel.next(Some(current)), Some(candidate.clone()));
                assert_eq!(carousel.bag, bag);
            }

            // The new bag still contains every member, including the externally
            // consumed image, but its first draw must be a different image.
            carousel.commit(candidate.clone());
            let mut seen = BTreeSet::from([candidate.clone()]);
            let mut current = candidate;
            for _ in 1..count {
                let next = carousel.next(Some(&current)).unwrap();
                assert_ne!(next, current);
                assert!(seen.insert(next.clone()));
                carousel.commit(next.clone());
                current = next;
            }
            assert_eq!(seen, paths.into_iter().collect());
            assert!(carousel.bag.is_empty());
        }
    }

    #[test]
    fn externally_current_entry_does_not_refill_before_other_entries_are_consumed() {
        let paths = paths(4);
        let mut carousel = Carousel::default();
        carousel.reconcile(&paths);
        carousel.commit(paths[0].clone());
        carousel.commit(paths[1].clone());
        carousel.bag = vec![paths[2].clone(), paths[3].clone()];

        assert_eq!(carousel.next(Some(&paths[3])), Some(paths[2].clone()));
        assert_eq!(carousel.bag.len(), 2);
        carousel.commit(paths[2].clone());
        assert_eq!(carousel.bag, vec![paths[3].clone()]);
        assert_ne!(carousel.next(Some(&paths[3])), Some(paths[3].clone()));
        assert_eq!(carousel.bag.len(), paths.len());
    }

    #[test]
    fn zero_and_one_member_and_unsuccessful_apply() {
        let mut carousel = Carousel::default();
        assert_eq!(carousel.next(None), None);
        assert_eq!(carousel.previous(None), None);
        let paths = paths(1);
        carousel.reconcile(&paths);
        for _ in 0..4 {
            assert_eq!(carousel.next(Some(&paths[0])), Some(paths[0].clone()));
        }
        assert!(carousel.history.is_empty());
        carousel.commit(paths[0].clone());
        assert_eq!(carousel.previous(Some(&paths[0])), None);
        assert_eq!(carousel.next(Some(&paths[0])), Some(paths[0].clone()));
        carousel.commit(paths[0].clone());
        assert_eq!(carousel.history.len(), 1);
        carousel.reconcile(&[]);
        assert_eq!(carousel.next(None), None);
        assert!(carousel.history.is_empty());
    }

    #[test]
    fn failed_selection_does_not_consume_and_round_trip_preserves_bag() {
        let mut carousel = Carousel::default();
        carousel.reconcile(&paths(4));
        let candidate = carousel.next(None).unwrap();
        assert_eq!(carousel.next(None), Some(candidate.clone()));
        assert_eq!(carousel.bag.len(), 4);
        let json = serde_json::to_string(&carousel).unwrap();
        let mut loaded: Carousel = serde_json::from_str(&json).unwrap();
        loaded.reconcile(&paths(4));
        assert_eq!(loaded.next(None), Some(candidate.clone()));
        loaded.commit(candidate);
        assert_eq!(loaded.bag.len(), 3);
    }

    #[test]
    fn previous_forward_only_move_on_commit() {
        let paths = paths(4);
        let mut carousel = Carousel::default();
        carousel.reconcile(&paths);
        for path in &paths[..3] {
            carousel.commit(path.clone());
        }
        let bag = carousel.bag.clone();
        assert_eq!(carousel.previous(Some(&paths[2])), Some(paths[1].clone()));
        assert_eq!(carousel.cursor, Some(2));
        assert_eq!(carousel.previous(Some(&paths[2])), Some(paths[1].clone()));
        carousel.commit(paths[1].clone());
        assert_eq!(carousel.next(Some(&paths[1])), Some(paths[2].clone()));
        assert_eq!(carousel.cursor, Some(1));
        carousel.commit(paths[2].clone());
        assert_eq!(carousel.bag, bag);
        let previous = carousel.previous(Some(&paths[2])).unwrap();
        carousel.commit(previous);
        carousel.commit(paths[3].clone());
        assert_eq!(
            carousel.history,
            vec![paths[0].clone(), paths[1].clone(), paths[3].clone()]
        );
    }

    #[test]
    fn reconcile_removes_missing_images_and_adds_without_resurrecting_consumed() {
        let paths = paths(4);
        let mut carousel = Carousel::default();
        carousel.reconcile(&paths[..3]);
        carousel.commit(paths[0].clone());
        carousel.commit(paths[1].clone());
        carousel.reconcile(&[
            paths[0].clone(),
            paths[2].clone(),
            paths[3].clone(),
            paths[3].clone(),
        ]);
        assert_eq!(carousel.history, vec![paths[0].clone()]);
        assert_eq!(carousel.cursor, Some(0));
        assert_eq!(
            carousel.bag.iter().cloned().collect::<BTreeSet<_>>(),
            [paths[2].clone(), paths[3].clone()].into_iter().collect()
        );
        assert_eq!(carousel.previous(Some(&paths[0])), None);
    }

    #[test]
    fn history_is_bounded_and_serializes_navigation() {
        let paths = paths(2);
        let mut carousel = Carousel::default();
        carousel.reconcile(&paths);
        for index in 0..300 {
            carousel.commit(paths[index % 2].clone());
        }
        assert_eq!(carousel.history.len(), HISTORY_LIMIT);
        let previous = carousel.previous(Some(&paths[1])).unwrap();
        carousel.commit(previous.clone());
        let mut restored: Carousel =
            serde_json::from_str(&serde_json::to_string(&carousel).unwrap()).unwrap();
        restored.reconcile(&paths);
        assert_eq!(restored.next(Some(&previous)), Some(paths[1].clone()));
        for _ in 0..HISTORY_LIMIT - 2 {
            let path = restored.previous(None).unwrap();
            restored.commit(path);
        }
        assert_eq!(restored.previous(None), None);
    }
}
