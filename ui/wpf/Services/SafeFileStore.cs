using System;
using System.Collections.Generic;
using System.IO;
using System.Text.Json;
using System.Threading.Tasks;

namespace GaoxiaoVideo.Services;

public static class SafeFileStore
{
	private sealed class PendingWrite
	{
		public required Func<string> Serialize { get; init; }
		public required Func<string, bool> Validate { get; init; }
		public long DueTicks { get; init; }
		public long Version { get; init; }
	}

	private sealed class FileState
	{
		public readonly object SyncRoot = new object();
		public PendingWrite? Pending;
		public long Version;
		public bool WorkerRunning;
	}

	private static readonly JsonSerializerOptions DefaultOptions = new JsonSerializerOptions
	{
		WriteIndented = true
	};

	private static readonly object _globalLock = new object();
	private static readonly Dictionary<string, FileState> _files = new Dictionary<string, FileState>(StringComparer.OrdinalIgnoreCase);

	private static FileState GetFileState(string normalizedPath)
	{
		lock (_globalLock)
		{
			if (!_files.TryGetValue(normalizedPath, out FileState? state))
			{
				state = new FileState();
				_files[normalizedPath] = state;
			}
			return state;
		}
	}

	public static T? Load<T>(string filePath, out bool hadError) where T : class
	{
		hadError = false;
		try
		{
			filePath = Path.GetFullPath(filePath);
			lock (GetFileState(filePath).SyncRoot)
			{
				if (TryRead<T>(filePath, out T? data) && data != null)
				{
					return data;
				}
				hadError = File.Exists(filePath);
				if (TryRead<T>(filePath + ".bak", out T? bak) && bak != null)
				{
					// Preserve the valid backup when repairing a corrupt main file.
					SaveSerialized(filePath, JsonSerializer.Serialize(bak, DefaultOptions), Validate<T>, preserveBackup: true);
					return bak;
				}
			}
		}
		catch
		{
			hadError = true;
		}
		return null;
	}

	public static bool Save<T>(string filePath, T data, JsonSerializerOptions? options = null) where T : class
	{
		try
		{
			filePath = Path.GetFullPath(filePath);
			FileState state = GetFileState(filePath);
			lock (state.SyncRoot)
			{
				// A newer explicit save supersedes even an older write still being serialized.
				state.Version++;
				state.Pending = null;
				return SaveSerialized(filePath, JsonSerializer.Serialize(data, options ?? DefaultOptions), Validate<T>);
			}
		}
		catch
		{
			return false;
		}
	}

	public static bool BackupTo(string filePath, string targetDir)
	{
		try
		{
			filePath = Path.GetFullPath(filePath);
			lock (GetFileState(filePath).SyncRoot)
			{
				if (!File.Exists(filePath))
				{
					return false;
				}
				Directory.CreateDirectory(targetDir);
				string name = Path.GetFileName(filePath);
				File.Copy(filePath, Path.Combine(targetDir, name), overwrite: true);
				if (File.Exists(filePath + ".bak"))
				{
					File.Copy(filePath + ".bak", Path.Combine(targetDir, name + ".bak"), overwrite: true);
				}
				return true;
			}
		}
		catch
		{
			return false;
		}
	}

	private static bool TryRead<T>(string path, out T? data) where T : class
	{
		data = null;
		try
		{
			if (!File.Exists(path))
			{
				return false;
			}
			string json = File.ReadAllText(path);
			if (string.IsNullOrWhiteSpace(json))
			{
				return false;
			}
			data = JsonSerializer.Deserialize<T>(json);
			return data != null;
		}
		catch
		{
			return false;
		}
	}

	private static bool Validate<T>(string path) where T : class => TryRead<T>(path, out _);

	// The caller holds the path lock. All main/.bak/.tmp mutations use this routine.
	private static bool SaveSerialized(string filePath, string json, Func<string, bool> validate, bool preserveBackup = false)
	{
		string tmpPath = filePath + ".tmp";
		try
		{
			string? dir = Path.GetDirectoryName(filePath);
			if (!string.IsNullOrEmpty(dir))
			{
				Directory.CreateDirectory(dir);
			}
			File.WriteAllText(tmpPath, json);
			if (!validate(tmpPath))
			{
				return false;
			}
			if (File.Exists(filePath))
			{
				File.Replace(tmpPath, filePath, preserveBackup ? null : filePath + ".bak", ignoreMetadataErrors: true);
			}
			else
			{
				File.Move(tmpPath, filePath);
				if (!preserveBackup)
				{
					try
					{
						File.Copy(filePath, filePath + ".bak", overwrite: true);
					}
					catch
					{
					}
				}
			}
			return true;
		}
		catch
		{
			return false;
		}
		finally
		{
			TryDelete(tmpPath);
		}
	}

	private static void TryDelete(string path)
	{
		try
		{
			File.Delete(path);
		}
		catch
		{
		}
	}

	public static void SaveDebouncedAsync<T>(string filePath, T data, int debounceMs = 600, JsonSerializerOptions? options = null) where T : class
	{
		filePath = Path.GetFullPath(filePath);
		FileState state = GetFileState(filePath);
		bool needStart;
		lock (state.SyncRoot)
		{
			state.Pending = new PendingWrite
			{
				Serialize = () => JsonSerializer.Serialize(data, options ?? DefaultOptions),
				Validate = Validate<T>,
				DueTicks = DateTime.UtcNow.Ticks + TimeSpan.FromMilliseconds(Math.Max(0, debounceMs)).Ticks,
				Version = ++state.Version
			};
			needStart = !state.WorkerRunning;
			state.WorkerRunning = true;
		}
		if (needStart)
		{
			_ = Task.Run(() => ProcessPendingLoopAsync(filePath, state));
		}
	}

	private static async Task ProcessPendingLoopAsync(string filePath, FileState state)
	{
		while (true)
		{
			PendingWrite pending;
			long wait;
			lock (state.SyncRoot)
			{
				if (state.Pending == null)
				{
					state.WorkerRunning = false;
					return;
				}
				pending = state.Pending;
				wait = pending.DueTicks - DateTime.UtcNow.Ticks;
			}
			if (wait > 0)
			{
				await Task.Delay((int)Math.Min(500, Math.Max(1, TimeSpan.FromTicks(wait).TotalMilliseconds))).ConfigureAwait(false);
				continue;
			}
			CommitPending(filePath, state, pending);
			lock (state.SyncRoot)
			{
				if (ReferenceEquals(state.Pending, pending))
				{
					// Keep a failed current snapshot for an explicit flush retry, without a busy retry loop.
					state.WorkerRunning = false;
					return;
				}
			}
		}
	}

	private static void CommitPending(string filePath, FileState state, PendingWrite pending)
	{
		try
		{
			lock (state.SyncRoot)
			{
				if (state.Version != pending.Version || !ReferenceEquals(state.Pending, pending))
				{
					return;
				}
			}
			string json = pending.Serialize();
			lock (state.SyncRoot)
			{
				if (state.Version != pending.Version || !ReferenceEquals(state.Pending, pending))
				{
					return;
				}
				if (SaveSerialized(filePath, json, pending.Validate))
				{
					state.Pending = null;
				}
			}
		}
		catch
		{
		}
	}

	public static void FlushPending(string filePath)
	{
		filePath = Path.GetFullPath(filePath);
		FileState state = GetFileState(filePath);
		PendingWrite? pending;
		lock (state.SyncRoot)
		{
			pending = state.Pending;
		}
		if (pending != null)
		{
			CommitPending(filePath, state, pending);
		}
	}

	public static void FlushAllPending()
	{
		List<string> paths;
		lock (_globalLock)
		{
			paths = new List<string>(_files.Keys);
		}
		foreach (string path in paths)
		{
			FlushPending(path);
		}
	}
}
