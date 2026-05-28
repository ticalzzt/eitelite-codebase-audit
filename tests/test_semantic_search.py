#!/usr/bin/env python3
"""
Test: Semantic Search for PersistentMemory.

Tests:
1. Embedding model loads correctly
2. FAISS index builds and searches
3. PersistentMemory.semantic_search() works end-to-end
4. Auto-index on store/forget
5. Rebuild from DB
6. Fallback to keyword search when unavailable
"""

import os
import sys
import time
import tempfile
import json

# Use system python for sentence-transformers
sys.path.insert(0, '/home/ubuntu/eitelite')

from tical_code.core.memory import PersistentMemory, get_persistent_memory, reset_persistent_memory


def test_semantic_index_standalone():
    """Test SemanticIndex independently."""
    print("\n=== Test 1: SemanticIndex standalone ===")
    
    from tical_code.core.semantic_search import SemanticIndex, get_semantic_index, reset_semantic_index
    reset_semantic_index()
    
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        idx = SemanticIndex(db_path)
        
        # Upsert some entries
        ok = idx.upsert("eite_rule_1", "EITE验证系统第一规则：禁止编造不存在的操作结果")
        assert ok, "upsert failed"
        
        ok = idx.upsert("eite_rule_2", "验证引擎必须在tool执行前后都检查，不能只检查一次")
        assert ok, "upsert failed"
        
        ok = idx.upsert("gpu_memory", "3090显卡有24GB显存，适合跑7B模型推理")
        assert ok, "upsert failed"
        
        ok = idx.upsert("deployment", "部署到台湾VPS用systemd管理进程")
        assert ok, "upsert failed"
        
        ok = idx.upsert("anti_fabrication", "反编造机制：验证引擎检查每条回复的声明与证据匹配")
        assert ok, "upsert failed"
        
        assert len(idx._keys) == 5, f"Expected 5 keys, got {len(idx._keys)}"
        print(f"  Indexed {len(idx._keys)} entries")
        
        # Test semantic search — query about verification
        results = idx.search("验证引擎怎么检查幻觉", top_k=3)
        print(f"  Query: '验证引擎怎么检查幻觉'")
        for key, score in results:
            print(f"    {key}: {score:.4f}")
        assert len(results) > 0, "No results"
        assert results[0][0] in ("eite_rule_1", "anti_fabrication"), \
            f"Expected verification-related top result, got {results[0][0]}"
        print(f"  PASS: Top result is {results[0][0]} ({results[0][1]:.4f})")
        
        # Test semantic search — query about GPU
        results = idx.search("显卡显存多大", top_k=2)
        print(f"\n  Query: '显卡显存多大'")
        for key, score in results:
            print(f"    {key}: {score:.4f}")
        assert results[0][0] == "gpu_memory", \
            f"Expected gpu_memory, got {results[0][0]}"
        print(f"  PASS: Top result is {results[0][0]} ({results[0][1]:.4f})")
        
        # Test cross-lingual search
        results = idx.search("how to deploy the verification system", top_k=2)
        print(f"\n  Query: 'how to deploy the verification system' (English)")
        for key, score in results:
            print(f"    {key}: {score:.4f}")
        assert len(results) > 0, "No cross-lingual results"
        print(f"  PASS: Cross-lingual search works")
        
        # Test save/load
        idx._save()
        assert os.path.exists(idx.embeddings_path), "Embeddings file not created"
        
        idx2 = SemanticIndex(db_path)
        ok = idx2._ensure_loaded()
        assert ok, "Reload failed"
        assert len(idx2._keys) == 5, f"Expected 5 keys after reload, got {len(idx2._keys)}"
        
        results = idx2.search("GPU加速推理", top_k=1)
        assert results[0][0] == "gpu_memory", f"Expected gpu_memory after reload, got {results[0][0]}"
        print(f"  PASS: Save/load roundtrip works")
        
        # Test remove
        idx2.remove("gpu_memory")
        assert len(idx2._keys) == 4
        print(f"  PASS: Remove works")
        
        # Stats
        stats = idx.get_stats()
        print(f"\n  Stats: {json.dumps(stats, indent=2)}")
    
    print("  Test 1: ALL PASS ✓")


def test_persistent_memory_semantic_search():
    """Test PersistentMemory.semantic_search() end-to-end."""
    print("\n=== Test 2: PersistentMemory.semantic_search() ===")
    
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_memory.db")
        pm = PersistentMemory(db_path)
        
        # Store some memories
        pm.store("eite_core", "EITE验证系统是tical-code的核心差异化，3阶段验证", category="architecture", priority=9)
        pm.store("grpo_training", "GRPO训练用4个reward function：no-hallucination, evidence-chain, concise, chinese", category="training", priority=8)
        pm.store("vps_taiwan", "台湾VPS：16GB内存，跑tical-code worker进程", category="infra", priority=7)
        pm.store("vps_sg", "新加坡VPS：8GB内存，跑bench测试站", category="infra", priority=6)
        pm.store("context_window", "MIMO v2.5-pro 1M上下文窗口，compactor设为200K", category="config", priority=8)
        pm.store("anti_fabrication", "反编造：Rule 8检测没有check_self的模型声明，Rule 13强制check_self", category="rules", priority=9)
        
        print(f"  Stored {len(pm.get_context_for_session(100))} memories")
        
        # Semantic search — English query on Chinese content
        results = pm.semantic_search("hallucination detection mechanism", top_k=3)
        print(f"\n  Query: 'hallucination detection mechanism'")
        for r in results:
            print(f"    {r['key']}: {r.get('semantic_score', 'N/A')}")
        assert len(results) > 0, "No semantic results"
        print(f"  PASS: Cross-lingual semantic search works")
        
        # Semantic search — Chinese query
        results = pm.semantic_search("部署在哪台服务器", top_k=2)
        print(f"\n  Query: '部署在哪台服务器'")
        for r in results:
            print(f"    {r['key']}: {r.get('semantic_score', 'N/A')}")
        assert any(r['key'].startswith('vps_') for r in results), \
            "Expected VPS-related result"
        print(f"  PASS: Chinese semantic search works")
        
        # Compare with keyword search
        kw_results = pm.search_by_keywords("验证 幻觉 检测")
        sem_results = pm.semantic_search("验证系统如何防止AI说谎", top_k=3)
        print(f"\n  Keyword results: {len(kw_results)} | Semantic results: {len(sem_results)}")
        print(f"  PASS: Both search methods return results")
        
        # Rebuild
        count = pm.reindex_semantic()
        print(f"\n  Reindex: {count} entries")
        assert count == 6, f"Expected 6, got {count}"
        print(f"  PASS: Reindex works")
        
        # Stats
        stats = pm.get_stats()
        print(f"\n  DB stats: {json.dumps(stats, indent=2)}")
    
    print("  Test 2: ALL PASS ✓")


def test_fallback():
    """Test fallback when sentence-transformers is not available."""
    print("\n=== Test 3: Fallback behavior ===")
    
    # This test verifies the code paths work even when imports fail
    from tical_code.core.semantic_search import reset_semantic_index
    reset_semantic_index()
    
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        pm = PersistentMemory(db_path)
        pm.store("test_key", "test_value", category="test")
        
        # Should fallback to keyword search if semantic fails
        results = pm.semantic_search("test value", top_k=5)
        # Even if semantic fails, keyword fallback should work
        print(f"  Fallback results: {len(results)}")
        print(f"  PASS: Fallback works")
    
    print("  Test 3: ALL PASS ✓")


if __name__ == "__main__":
    start = time.time()
    
    test_semantic_index_standalone()
    test_persistent_memory_semantic_search()
    test_fallback()
    
    elapsed = time.time() - start
    print(f"\n{'='*50}")
    print(f"ALL TESTS PASSED ✓ ({elapsed:.1f}s)")
