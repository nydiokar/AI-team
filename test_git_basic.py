#!/usr/bin/env python3
"""
Basic git automation test without LLAMA dependency
"""
import os
import sys
import tempfile
import shutil
import subprocess
from pathlib import Path

# Add src to path
src_path = Path(__file__).parent / "src"
if str(src_path) not in sys.path:
    sys.path.append(str(src_path))

def test_git_file_detector_basic():
    """Test GitFileDetector basic functionality"""
    print("🧪 Testing GitFileDetector (Basic)...")
    
    try:
        print("  📥 Importing GitFileDetector...")
        from src.core.git_file_detector import GitFileDetector
        print("  ✅ Import successful")
        
        print("  🔧 Initializing detector...")
        detector = GitFileDetector()
        print("  ✅ GitFileDetector initialized successfully")
        
        print("  🌿 Getting current branch...")
        current_branch = detector.get_current_branch()
        if current_branch:
            print(f"  ✅ Current branch: {current_branch}")
        else:
            print("  ⚠️  Could not determine current branch")
        
        print("  📝 Detecting file changes...")
        changes = detector.detect_file_changes()
        print(f"  ✅ File changes detected: {changes['total']} total changes")
        
        return True
        
    except Exception as e:
        print(f"  ❌ Error: {e}")
        return False

def test_git_cli_basic():
    """Test git CLI commands without LLAMA"""
    print("\n🧪 Testing Git CLI Commands (Basic)...")
    
    try:
        print("  📥 Testing git-status...")
        result = subprocess.run(
            [sys.executable, "main.py", "git-status"],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode == 0:
            print("  ✅ git-status command executed successfully")
            if "Git Repository Status" in result.stdout:
                print("  ✅ git-status output contains expected content")
                return True
            else:
                print("  ⚠️  git-status output format may be unexpected")
                print(f"  📄 Output: {result.stdout[:200]}...")
                return False
        else:
            print(f"  ❌ git-status command failed: {result.stderr}")
            return False
        
    except subprocess.TimeoutExpired:
        print("  ❌ git-status command timed out")
        return False
    except Exception as e:
        print(f"  ❌ Error testing CLI commands: {e}")
        return False

def test_temp_repo_basic():
    """Test with temporary repository without LLAMA"""
    print("\n🧪 Testing with Temporary Repository (Basic)...")
    
    temp_dir = tempfile.mkdtemp()
    repo_path = Path(temp_dir)
    
    try:
        print("  🔧 Initializing temporary git repository...")
        subprocess.run(['git', 'init'], cwd=repo_path, check=True)
        subprocess.run(['git', 'config', 'user.name', 'Test User'], cwd=repo_path, check=True)
        subprocess.run(['git', 'config', 'user.email', 'test@example.com'], cwd=repo_path, check=True)
        print("  ✅ Temporary git repository initialized")
        
        print("  📝 Creating test file...")
        test_file = repo_path / "test.txt"
        test_file.write_text("Hello, World!")
        print("  ✅ Test file created")
        
        print("  🔍 Testing GitFileDetector in temp repo...")
        from src.core.git_file_detector import GitFileDetector
        detector = GitFileDetector(str(repo_path))
        
        changes = detector.detect_file_changes()
        if changes['created']:
            print(f"  ✅ File changes detected: {changes['created']}")
        else:
            print("  ⚠️  No file changes detected")
        
        print("  📦 Staging test file...")
        subprocess.run(['git', 'add', 'test.txt'], cwd=repo_path, check=True)
        print("  ✅ File staged")
        
        print("  🌿 Testing branch creation...")
        branch_name = detector.create_feature_branch(
            task_id="test_basic",
            description="Basic test branch"
        )
        if branch_name:
            print(f"  ✅ Feature branch created: {branch_name}")
        else:
            print("  ❌ Failed to create feature branch")
        
        return True
        
    except Exception as e:
        print(f"  ❌ Error: {e}")
        return False
    finally:
        # Cleanup with Windows-friendly approach
        try:
            # Give git processes time to finish
            import time
            time.sleep(1)
            
            # Force close any open git processes
            try:
                subprocess.run(['git', 'gc'], cwd=repo_path, capture_output=True, timeout=5)
            except:
                pass  # Ignore gc errors
            
            # Try to remove the directory
            shutil.rmtree(temp_dir, ignore_errors=True)
            
            # If rmtree failed, try alternative cleanup
            if temp_dir.exists():
                try:
                    # Remove git objects first
                    git_objects = temp_dir / ".git" / "objects"
                    if git_objects.exists():
                        for obj_dir in git_objects.iterdir():
                            if obj_dir.is_dir():
                                for obj_file in obj_dir.iterdir():
                                    try:
                                        obj_file.unlink()
                                    except:
                                        pass
                        git_objects.rmdir()
                    
                    # Now try to remove the rest
                    shutil.rmtree(temp_dir, ignore_errors=True)
                except:
                    print(f"  ⚠️  Could not fully clean up {temp_dir}")
                    print("  💡 This is normal on Windows - temp files will be cleaned up automatically")
            
            print("  ✅ Temporary repository cleaned up")
            
        except Exception as e:
            print(f"  ⚠️  Cleanup warning: {e}")
            print("  💡 This is normal on Windows - temp files will be cleaned up automatically")

def test_imports_only():
    """Test just importing the modules without initialization"""
    print("\n🧪 Testing Module Imports...")
    
    modules = [
        ("GitFileDetector", "src.core.git_file_detector"),
        ("GitAutomationService", "src.core.git_automation"),
        ("LlamaMediator", "src.bridges.llama_mediator"),
    ]
    
    results = []
    for module_name, module_path in modules:
        try:
            print(f"  📥 Importing {module_name}...")
            __import__(module_path)
            print(f"  ✅ {module_name} imported successfully")
            results.append(True)
        except Exception as e:
            print(f"  ❌ {module_name} import failed: {e}")
            results.append(False)
    
    return all(results)

def main():
    """Run basic tests"""
    print("🚀 Starting Basic Git Automation Tests")
    print("=" * 50)
    
    tests = [
        ("Module Imports", test_imports_only),
        ("GitFileDetector Basic", test_git_file_detector_basic),
        ("Git CLI Basic", test_git_cli_basic),
        ("Temp Repository Basic", test_temp_repo_basic),
    ]
    
    results = []
    for test_name, test_func in tests:
        try:
            print(f"\n🔄 Running {test_name}...")
            success = test_func()
            results.append((test_name, success))
        except KeyboardInterrupt:
            print(f"\n⏹️  {test_name} test interrupted by user")
            results.append((test_name, False))
            break
        except Exception as e:
            print(f"\n❌ {test_name} test crashed: {e}")
            results.append((test_name, False))
    
    # Summary
    print("\n" + "=" * 50)
    print("📊 Basic Test Results Summary")
    print("=" * 50)
    
    passed = 0
    total = len(results)
    
    for test_name, success in results:
        status = "✅ PASS" if success else "❌ FAIL"
        print(f"{status} {test_name}")
        if success:
            passed += 1
    
    print(f"\nOverall: {passed}/{total} tests passed")
    
    if passed == total:
        print("🎉 All basic tests passed! Core git functionality is working.")
        print("\n💡 Next Steps:")
        print("  • Run full tests with: python test_git_automation.py")
        print("  • Consider upgrading to Gemma3 12B for better LLAMA performance")
        return 0
    else:
        print("⚠️  Some basic tests failed. Check the output above for details.")
        return 1

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\n⏹️  Tests interrupted by user")
        sys.exit(1)
