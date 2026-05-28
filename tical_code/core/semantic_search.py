"""Semantic Search — embedding-based memory retrieval.

Uses sentence-transformers + FAISS for vector similarity search.
Falls back to keyword search when embeddings are unavailable.

Model: paraphrase-multilingual-MiniLM-L12-v2 (384-dim, ~470MB, CPU-friendly)
Supports: Chinese + English + 50 other languages
"""

import json
import os
import logging
import sqlite3
from typing import List, Dict, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy-loaded globals (avoid import-time model load)
# ---------------------------------------------------------------------------

_model = None
_index = None
MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
EMBEDDING_DIM = 384


def _get_model():
    """Lazy-load sentence-transformers model."""
    global _model
    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer
            _model = SentenceTransformer(MODEL_NAME)
            logger.info(f"[SemanticSearch] Loaded model: {MODEL_NAME}")
        except ImportError:
            logger.warning("[SemanticSearch] sentence-transformers not installed, "
                           "semantic search disabled")
            return None
        except Exception as e:
            logger.error(f"[SemanticSearch] Failed to load model: {e}")
            return None
    return _model


def _get_index():
    """Lazy-load or create FAISS index."""
    global _index
    if _index is None:
        try:
            import faiss
            _index = faiss.IndexFlatIP(EMBEDDING_DIM)  # Inner product (cosine after norm)
            logger.info("[SemanticSearch] Created FAISS index")
        except ImportError:
            logger.warning("[SemanticSearch] faiss not installed, semantic search disabled")
            return None
    return _index


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------

class SemanticIndex:
    """
    Manages embeddings for PersistentMemory.
    
    Embeddings are stored in a numpy file alongside the SQLite DB.
    FAISS index is rebuilt on startup from the stored embeddings.
    """

    def __init__(self, db_path: str):
        """
        Args:
            db_path: Path to the SQLite memory.db file.
                     Embeddings stored as <db_path>.embeddings.npz
        """
        self.db_path = db_path
        self.embeddings_path = db_path + ".embeddings.npz"
        self.keys_path = db_path + ".keys.json"
        
        self._model = None
        self._index = None
        self._keys: List[str] = []
        self._vectors: Optional[np.ndarray] = None
        
        self._loaded = False

    def _ensure_loaded(self):
        """Load index from disk on first access."""
        if self._loaded:
            return True
        
        self._model = _get_model()
        self._index = _get_index()
        
        if self._model is None or self._index is None:
            return False
        
        # Load stored embeddings
        if os.path.exists(self.embeddings_path) and os.path.exists(self.keys_path):
            try:
                data = np.load(self.embeddings_path)
                self._vectors = data['embeddings']
                
                with open(self.keys_path, 'r') as f:
                    self._keys = json.load(f)
                
                assert len(self._keys) == self._vectors.shape[0], \
                    f"Key/vector mismatch: {len(self._keys)} keys vs {self._vectors.shape[0]} vectors"
                
                # Rebuild FAISS index
                self._index.reset()
                if len(self._vectors) > 0:
                    self._index.add(self._vectors)
                
                self._loaded = True
                logger.info(f"[SemanticSearch] Loaded {len(self._keys)} embeddings")
                return True
            except Exception as e:
                logger.error(f"[SemanticSearch] Failed to load embeddings: {e}")
                # Reset and rebuild
                self._keys = []
                self._vectors = None
                self._index.reset()
        
        self._loaded = True
        return True

    def _save(self):
        """Persist embeddings to disk."""
        if self._vectors is None or len(self._keys) == 0:
            return
        
        try:
            np.savez_compressed(self.embeddings_path, embeddings=self._vectors)
            with open(self.keys_path, 'w') as f:
                json.dump(self._keys, f)
            logger.debug(f"[SemanticSearch] Saved {len(self._keys)} embeddings")
        except Exception as e:
            logger.error(f"[SemanticSearch] Failed to save embeddings: {e}")

    def encode_texts(self, texts: List[str]) -> np.ndarray:
        """Encode texts to normalized embeddings."""
        if not self._ensure_loaded() or self._model is None:
            return np.array([])
        
        embeddings = self._model.encode(texts, show_progress_bar=False,
                                        normalize_embeddings=True)
        return embeddings.astype(np.float32)

    def upsert(self, key: str, text: str) -> bool:
        """Add or update embedding for a memory entry."""
        if not self._ensure_loaded():
            return False
        
        embedding = self.encode_texts([text])
        if embedding.shape[0] == 0:
            return False
        
        vector = embedding[0]
        
        # Check if key already exists
        if key in self._keys:
            idx = self._keys.index(key)
            # Update vector in-place
            if self._vectors is not None:
                self._vectors[idx] = vector
            # Rebuild FAISS (can't update in-place easily)
            self._index.reset()
            self._index.add(self._vectors)
        else:
            # Append
            self._keys.append(key)
            if self._vectors is None:
                self._vectors = vector.reshape(1, -1)
            else:
                self._vectors = np.vstack([self._vectors, vector.reshape(1, -1)])
            self._index.add(vector.reshape(1, -1))
        
        return True

    def remove(self, key: str) -> bool:
        """Remove embedding for a memory entry."""
        if key not in self._keys:
            return False
        
        idx = self._keys.index(key)
        self._keys.pop(idx)
        self._vectors = np.delete(self._vectors, idx, axis=0)
        
        # Rebuild FAISS
        self._index.reset()
        if len(self._vectors) > 0:
            self._index.add(self._vectors)
        
        return True

    def search(self, query: str, top_k: int = 5,
               min_score: float = 0.3) -> List[Tuple[str, float]]:
        """
        Semantic search: find most similar memories by meaning.
        
        Returns:
            List of (key, similarity_score) tuples, sorted by score desc.
        """
        if not self._ensure_loaded() or self._model is None:
            return []
        
        if len(self._keys) == 0:
            return []
        
        query_vec = self.encode_texts([query])
        if query_vec.shape[0] == 0:
            return []
        
        # Search
        scores, indices = self._index.search(query_vec, min(top_k, len(self._keys)))
        
        results = []
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0 or idx >= len(self._keys):
                continue
            if score < min_score:
                continue
            results.append((self._keys[idx], float(score)))
        
        return results

    def rebuild_from_db(self, db_path: str, batch_size: int = 100):
        """
        Rebuild the entire embedding index from SQLite.
        Called on first run or after schema migration.
        """
        if not self._ensure_loaded():
            logger.warning("[SemanticSearch] Cannot rebuild: model/index unavailable")
            return
        
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cursor.execute("SELECT key, value FROM memories")
            rows = cursor.fetchall()
            conn.close()
            
            if not rows:
                logger.info("[SemanticSearch] No memories to index")
                return
            
            logger.info(f"[SemanticSearch] Rebuilding index for {len(rows)} memories...")
            
            all_keys = []
            all_texts = []
            
            for row in rows:
                key = row['key']
                value = row['value'] or ''
                # Combine key + value for embedding (key provides context)
                text = f"{key}: {value}"
                all_keys.append(key)
                all_texts.append(text)
            
            # Encode in batches
            all_embeddings = []
            for i in range(0, len(all_texts), batch_size):
                batch = all_texts[i:i + batch_size]
                emb = self.encode_texts(batch)
                all_embeddings.append(emb)
            
            if all_embeddings:
                self._vectors = np.vstack(all_embeddings)
                self._keys = all_keys
                
                # Rebuild FAISS
                self._index.reset()
                self._index.add(self._vectors)
                
                self._save()
                logger.info(f"[SemanticSearch] Index rebuilt: {len(self._keys)} entries")
        
        except Exception as e:
            logger.error(f"[SemanticSearch] Rebuild failed: {e}")

    def get_stats(self) -> Dict:
        """Get index statistics."""
        return {
            'total_embeddings': len(self._keys),
            'embedding_dim': EMBEDDING_DIM,
            'model': MODEL_NAME,
            'index_type': 'FAISS IndexFlatIP (cosine)',
            'loaded': self._loaded,
            'embeddings_file': self.embeddings_path,
        }


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_semantic_index: Optional[SemanticIndex] = None


def get_semantic_index(db_path: str = None) -> Optional[SemanticIndex]:
    """Get or create the global semantic index."""
    global _semantic_index
    if _semantic_index is None:
        if db_path is None:
            db_path = os.path.expanduser("~/.tical-code/memory.db")
        _semantic_index = SemanticIndex(db_path)
    return _semantic_index


def reset_semantic_index():
    """Reset the global semantic index (for testing)."""
    global _semantic_index
    _semantic_index = None
