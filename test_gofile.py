import os
import tempfile
import pytest

from gofile import human_size, normalize_hosts, compute_total_size, FALLBACK_UPLOAD_HOSTS, DEFAULT_UPLOAD_HOST

def test_human_size():
    assert human_size(500) == "500.00 B"
    assert human_size(1024) == "1.00 KB"
    assert human_size(1024 * 1024) == "1.00 MB"
    assert human_size(1024 * 1024 * 1024) == "1.00 GB"
    assert human_size(1024 * 1024 * 1024 * 1024) == "1.00 TB"
    assert human_size(1024 * 1024 * 1024 * 1024 * 1024) == "1.00 PB"
    # Fallback case (EB)
    assert human_size(1024 * 1024 * 1024 * 1024 * 1024 * 1024) == "1.00 EB"

def test_normalize_hosts():
    # Empty primary host without failover
    assert normalize_hosts("", False) == [DEFAULT_UPLOAD_HOST]

    # Empty primary host with failover
    assert normalize_hosts("", True) == FALLBACK_UPLOAD_HOSTS

    # Valid primary host without failover
    assert normalize_hosts("store1", False) == ["store1"]

    # Valid primary host with failover
    hosts = normalize_hosts("store1", True)
    assert hosts[0] == "store1"
    for fallback in FALLBACK_UPLOAD_HOSTS:
        if fallback != "store1":
            assert fallback in hosts

    # Valid primary host that is already in fallbacks, with failover
    # Assuming FALLBACK_UPLOAD_HOSTS is not empty
    if FALLBACK_UPLOAD_HOSTS:
        first_fallback = FALLBACK_UPLOAD_HOSTS[0]
        hosts = normalize_hosts(first_fallback, True)
        assert hosts[0] == first_fallback
        assert hosts.count(first_fallback) == 1
        assert len(hosts) == len(FALLBACK_UPLOAD_HOSTS)

def test_compute_total_size():
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create a few files
        file1 = os.path.join(temp_dir, "test1.txt")
        file2 = os.path.join(temp_dir, "test2.txt")

        with open(file1, "wb") as f:
            f.write(b"Hello World")  # 11 bytes

        with open(file2, "wb") as f:
            f.write(b"12345")  # 5 bytes

        # Also create a nested folder with a file
        nested_dir = os.path.join(temp_dir, "nested")
        os.makedirs(nested_dir)
        file3 = os.path.join(nested_dir, "test3.txt")

        with open(file3, "wb") as f:
            f.write(b"A" * 100)  # 100 bytes

        total_size = compute_total_size(temp_dir)
        assert total_size == 11 + 5 + 100

        # Test single file
        total_size_file1 = compute_total_size(file1)
        assert total_size_file1 == 11
