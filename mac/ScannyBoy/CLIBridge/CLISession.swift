import Darwin
import Foundation

/// One invocation of the CLI, and everything that belongs to it.
///
/// `docs/IMPLEMENTATION_PLAN.md` section 5.3: each invocation is an owned
/// object rather than global mutable state, process and stream state is
/// isolated for Swift 6 concurrency, stdout is streamed by line while stderr
/// is drained concurrently, and the output stream finishes exactly once —
/// after the process has terminated *and* both pipes have reached EOF.
public actor CLISession {
    public struct Configuration: Sendable {
        public var executable: URL
        public var arguments: [String]
        public var currentDirectory: URL?
        /// Replaces the child's whole environment when set; `nil` inherits
        /// this process's environment.
        public var environment: [String: String]?

        public init(
            executable: URL,
            arguments: [String] = [],
            currentDirectory: URL? = nil,
            environment: [String: String]? = nil
        ) {
            self.executable = executable
            self.arguments = arguments
            self.currentDirectory = currentDirectory
            self.environment = environment
        }
    }

    /// How long the app waits for a cooperative cancellation before forcing
    /// the issue (section 3.8).
    public static let defaultGracePeriod: Duration = .seconds(5)

    private let configuration: Configuration
    private var process: Process?
    /// Kept separately from `process`: signalling is done by process
    /// identifier, because `Process.isRunning` is not dependable here.
    private var childPID: pid_t = 0
    private var didStart = false
    private var hasTerminated = false
    private var didForceTerminate = false

    public init(configuration: Configuration) {
        self.configuration = configuration
    }

    /// Launches the child and returns its output stream.
    ///
    /// Throws `CLISessionFailure.launch` if the process cannot be started;
    /// that failure is deliberately separate from the stream, because a
    /// process that never ran has no completion to report.
    public func start() throws -> AsyncStream<CLISessionOutput> {
        guard !didStart else {
            throw CLISessionFailure.launch("this session has already been started")
        }
        didStart = true

        let process = Process()
        process.executableURL = configuration.executable
        process.arguments = configuration.arguments
        process.currentDirectoryURL = configuration.currentDirectory
        process.environment = configuration.environment

        let standardOutput = Pipe()
        let standardError = Pipe()
        process.standardOutput = standardOutput
        process.standardError = standardError
        // The CLI reads no input; giving it /dev/null keeps it from ever
        // blocking on a terminal this app does not have.
        process.standardInput = FileHandle.nullDevice

        do {
            try process.run()
        } catch {
            try? standardOutput.fileHandleForReading.close()
            try? standardError.fileHandleForReading.close()
            throw CLISessionFailure.launch(
                "could not launch \(configuration.executable.path): "
                    + error.localizedDescription
            )
        }
        self.process = process
        self.childPID = process.processIdentifier

        let (stream, continuation) = AsyncStream<CLISessionOutput>.makeStream(
            bufferingPolicy: .unbounded
        )

        // Separate serial queues, so a full stderr pipe can never stall
        // stdout or the other way round.
        let outputChunks = Self.chunks(
            from: standardOutput.fileHandleForReading,
            queue: DispatchQueue(label: "com.lonniesmith.scanny-boy.cli.stdout")
        )
        let errorChunks = Self.chunks(
            from: standardError.fileHandleForReading,
            queue: DispatchQueue(label: "com.lonniesmith.scanny-boy.cli.stderr")
        )

        let stdoutTask = Task {
            var assembler = LineAssembler()
            for await chunk in outputChunks {
                switch chunk {
                case .data(let data):
                    for line in assembler.append(data) {
                        Self.emitEvent(line, to: continuation)
                    }
                case .failure(let message):
                    continuation.yield(
                        .failure(.read(stream: .standardOutput, message: message))
                    )
                }
            }
            for line in assembler.flush() {
                Self.emitEvent(line, to: continuation)
            }
        }

        let stderrTask = Task {
            var assembler = LineAssembler()
            for await chunk in errorChunks {
                switch chunk {
                case .data(let data):
                    for line in assembler.append(data) {
                        continuation.yield(.log(line))
                    }
                case .failure(let message):
                    continuation.yield(
                        .failure(.read(stream: .standardError, message: message))
                    )
                }
            }
            for line in assembler.flush() {
                continuation.yield(.log(line))
            }
        }

        // Deliberately not cancelled below: it is what records that the
        // child has gone, which the signalling methods depend on.
        let exitTask = Task { [weak self] () -> (status: Int32, reason: CLITerminationReason) in
            let ended = await Self.waitForExit(process)
            await self?.noteTermination()
            return ended
        }

        let coordinator = Task { [weak self] in
            // All three, in this order: nothing is reported as finished while
            // a byte of output could still arrive.
            await stdoutTask.value
            await stderrTask.value
            let ended = await exitTask.value
            let forced = await self?.wasForciblyTerminated ?? false
            continuation.yield(
                .completed(
                    CLICompletion(
                        terminationStatus: ended.status,
                        terminationReason: ended.reason,
                        outcome: CLIOutcome(
                            terminationStatus: ended.status,
                            terminationReason: ended.reason,
                            forced: forced
                        )
                    )
                )
            )
            continuation.finish()
        }

        continuation.onTermination = { [weak self] _ in
            // Reached both on the ordinary `finish()` above and when the
            // consumer stops iterating — a cancelled Swift task, say. Closing
            // the reader tasks ends their byte streams, which closes the pipe
            // handles; terminating a child that is somehow still running
            // keeps a cancelled task from orphaning it.
            stdoutTask.cancel()
            stderrTask.cancel()
            coordinator.cancel()
            Task { await self?.stopChildIfNeeded() }
        }

        return stream
    }

    /// Asks the CLI to cancel cooperatively (section 3.8: the app requests
    /// cancellation with SIGTERM).
    public func requestCancellation() {
        signalChild(SIGTERM)
    }

    /// Kills the child outright. A forced stop cannot clean files, update the
    /// manifest, or emit a final event; the run is classified as cancelled
    /// locally and the next probe or convert recovers the output folder.
    ///
    /// The run is only recorded as forced when the signal was actually
    /// delivered, so a CLI that stopped on its own a moment earlier is still
    /// reported as the ordinary cooperative cancellation it was.
    public func forceTerminate() {
        if signalChild(SIGKILL) {
            didForceTerminate = true
        }
    }

    /// Requests cooperative cancellation and forces the issue if the CLI has
    /// not stopped by the end of the grace period.
    public func cancel(gracePeriod: Duration = CLISession.defaultGracePeriod) async {
        requestCancellation()
        try? await Task.sleep(for: gracePeriod)
        forceTerminate()
    }

    public var isRunning: Bool {
        didStart && !hasTerminated
    }

    private var wasForciblyTerminated: Bool {
        didForceTerminate
    }

    /// Sends one signal to the child, reporting whether it was delivered.
    ///
    /// Signalling goes through the process identifier rather than
    /// `Process.terminate()`, and the session tracks termination itself
    /// rather than consulting `Process.isRunning`: on macOS 14.6.1 with
    /// Xcode 16.2, `isRunning` was observed reporting `false` for a child
    /// that was still running, which silently turned cancellation into a
    /// no-op.
    @discardableResult
    private func signalChild(_ number: Int32) -> Bool {
        guard !hasTerminated, childPID > 0 else { return false }
        return Darwin.kill(childPID, number) == 0
    }

    private func noteTermination() {
        hasTerminated = true
    }

    private func stopChildIfNeeded() {
        signalChild(SIGTERM)
    }

    // MARK: - Reading

    private enum Chunk: Sendable {
        case data(Data)
        case failure(String)
    }

    /// Turns a pipe into an ordered stream of byte chunks.
    ///
    /// The blocking reads run on a caller-supplied serial queue rather than
    /// the cooperative pool, and the ordering guarantee of
    /// `AsyncStream.Continuation` is what keeps the bytes in sequence.
    ///
    /// `read(2)` is used directly rather than `FileHandle.read(upToCount:)`,
    /// which is not a streaming read: it blocks until it has the whole count
    /// or the pipe reaches EOF, so a long conversion would deliver nothing
    /// until 64 KiB had piled up. `read(2)` returns as soon as any bytes are
    /// there, which is what an event stream needs.
    private static func chunks(
        from handle: FileHandle,
        queue: DispatchQueue
    ) -> AsyncStream<Chunk> {
        let handle = UncheckedSendable(handle)
        let descriptor = handle.value.fileDescriptor
        return AsyncStream(bufferingPolicy: .unbounded) { continuation in
            queue.async {
                var buffer = [UInt8](repeating: 0, count: 65_536)
                reading: while true {
                    let count = buffer.withUnsafeMutableBytes {
                        Darwin.read(descriptor, $0.baseAddress, $0.count)
                    }
                    if count > 0 {
                        continuation.yield(.data(Data(buffer[0..<count])))
                    } else if count == 0 {
                        break reading
                    } else if errno == EINTR {
                        continue reading
                    } else {
                        // A closed handle is how this stream is stopped on
                        // purpose, so it is an ending rather than a failure.
                        if errno != EBADF {
                            continuation.yield(.failure(String(cString: strerror(errno))))
                        }
                        break reading
                    }
                }
                continuation.finish()
            }
            continuation.onTermination = { _ in
                try? handle.value.close()
            }
        }
    }

    private static func emitEvent(
        _ line: String,
        to continuation: AsyncStream<CLISessionOutput>.Continuation
    ) {
        let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        do {
            continuation.yield(.event(try CLIEvent(line: trimmed)))
        } catch let error as CLIEventDecodingError {
            continuation.yield(
                .failure(.decode(line: trimmed, reason: error.description))
            )
        } catch {
            continuation.yield(
                .failure(.decode(line: trimmed, reason: error.localizedDescription))
            )
        }
    }

    private static func waitForExit(
        _ process: Process
    ) async -> (status: Int32, reason: CLITerminationReason) {
        let process = UncheckedSendable(process)
        return await withCheckedContinuation { continuation in
            let resumer = OnceResumer(continuation)
            process.value.terminationHandler = { terminated in
                resumer.resume(
                    with: (terminated.terminationStatus, CLITerminationReason(terminated.terminationReason))
                )
            }
            // A process that finished before the handler was installed never
            // calls it. `OnceResumer` makes losing this race harmless.
            if !process.value.isRunning {
                resumer.resume(
                    with: (
                        process.value.terminationStatus,
                        CLITerminationReason(process.value.terminationReason)
                    )
                )
            }
        }
    }
}

/// Wraps a value that Foundation has not marked `Sendable` so it can cross
/// into a queue closure. Sound only where a single queue owns the value,
/// which is how `CLISession` uses it.
private struct UncheckedSendable<Value>: @unchecked Sendable {
    let value: Value

    init(_ value: Value) {
        self.value = value
    }
}

/// Resumes a continuation at most once, from any thread.
private final class OnceResumer<Value: Sendable>: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<Value, Never>?

    init(_ continuation: CheckedContinuation<Value, Never>) {
        self.continuation = continuation
    }

    func resume(with value: Value) {
        lock.lock()
        let pending = continuation
        continuation = nil
        lock.unlock()
        pending?.resume(returning: value)
    }
}

// MARK: - Output

/// One thing the CLI produced, or the fact that it ended.
///
/// `completed` always arrives last and exactly once for a process that
/// launched, so a consumer can drive its whole lifecycle off this one stream.
public enum CLISessionOutput: Sendable {
    case event(CLIEvent)
    /// One line of stderr. Human-readable log text, never parsed.
    case log(String)
    /// A read or decode failure, reported separately from normal completion.
    case failure(CLISessionFailure)
    case completed(CLICompletion)
}

public enum CLIStream: String, Sendable, Hashable {
    case standardOutput
    case standardError
}

public enum CLISessionFailure: Error, Sendable, Hashable {
    /// The process could not be started. Thrown from `start()`.
    case launch(String)
    /// A pipe could not be read.
    case read(stream: CLIStream, message: String)
    /// A stdout line was not a usable event. An *unknown* event type is not a
    /// decode failure; see `CLIEvent.Kind.unknown`.
    case decode(line: String, reason: String)
}

public enum CLITerminationReason: Sendable, Hashable {
    case exit
    case uncaughtSignal

    init(_ reason: Process.TerminationReason) {
        self = reason == .uncaughtSignal ? .uncaughtSignal : .exit
    }
}

/// How a run ended, in the app's terms.
public enum CLIOutcome: Sendable, Hashable {
    /// Exit 0.
    case success
    /// Exit 1, or any other exit status the contract does not name.
    case failure
    /// Exit 2.
    case usageError
    /// The user cancelled. `forced` is true when this app had to kill the CLI
    /// after the grace period, which means no manifest update and no final
    /// event — the next run recovers the output folder.
    case cancelled(forced: Bool)
    /// Terminated by a signal other than SIGTERM.
    case terminatedBySignal(Int32)

    init(terminationStatus: Int32, terminationReason: CLITerminationReason, forced: Bool) {
        switch terminationReason {
        case .uncaughtSignal:
            // A user-requested cancellation counts as cancelled whether the
            // CLI exited 143 itself or was reported as terminated by signal
            // 15 (section 3.8).
            if forced {
                self = .cancelled(forced: true)
            } else if terminationStatus == SIGTERM {
                self = .cancelled(forced: false)
            } else {
                self = .terminatedBySignal(terminationStatus)
            }
        case .exit:
            switch terminationStatus {
            case 0: self = .success
            case 2: self = .usageError
            case 143: self = .cancelled(forced: false)
            default: self = .failure
            }
        }
    }
}

public struct CLICompletion: Sendable, Hashable {
    public let terminationStatus: Int32
    public let terminationReason: CLITerminationReason
    public let outcome: CLIOutcome
}
