import AppKit
import Carbon.HIToolbox

/// Registers a single global hotkey via Carbon's `RegisterEventHotKey`. This needs **no**
/// Accessibility permission (unlike a `CGEventTap`), which is why it's the right tool for
/// "open my search panel from anywhere". Re-`register(_:)` to change the key live.
final class CarbonHotkey {
    var onPressed: (() -> Void)?

    private var hotKeyRef: EventHotKeyRef?
    private var eventHandler: EventHandlerRef?
    private let identifier: UInt32
    private static var nextID: UInt32 = 1
    private static let signature: OSType = 0x47_41_4E_53 // 'GANS'

    init() {
        self.identifier = CarbonHotkey.nextID
        CarbonHotkey.nextID += 1
    }

    deinit { unregister() }

    /// Registers (replacing any previous registration) the given key + modifiers.
    @discardableResult
    func register(_ spec: HotkeySpec) -> Bool {
        unregister()

        var eventType = EventTypeSpec(eventClass: OSType(kEventClassKeyboard),
                                      eventKind: UInt32(kEventHotKeyPressed))
        let callback: EventHandlerUPP = { _, event, userData in
            guard let userData, let event else { return OSStatus(eventNotHandledErr) }
            let me = Unmanaged<CarbonHotkey>.fromOpaque(userData).takeUnretainedValue()
            var pressedID = EventHotKeyID()
            let status = GetEventParameter(event, EventParamName(kEventParamDirectObject),
                                           EventParamType(typeEventHotKeyID), nil,
                                           MemoryLayout<EventHotKeyID>.size, nil, &pressedID)
            guard status == noErr,
                  pressedID.signature == CarbonHotkey.signature,
                  pressedID.id == me.identifier else {
                return OSStatus(eventNotHandledErr)
            }
            DispatchQueue.main.async { me.onPressed?() }
            return noErr
        }

        let installStatus = InstallEventHandler(GetApplicationEventTarget(), callback, 1, &eventType,
                                                Unmanaged.passUnretained(self).toOpaque(), &eventHandler)
        guard installStatus == noErr else {
            Log.hotkey.error("InstallEventHandler failed: \(installStatus)")
            return false
        }

        let hotKeyID = EventHotKeyID(signature: Self.signature, id: identifier)
        let registerStatus = RegisterEventHotKey(spec.keyCode, spec.modifiers, hotKeyID,
                                                 GetApplicationEventTarget(), 0, &hotKeyRef)
        guard registerStatus == noErr else {
            Log.hotkey.error("RegisterEventHotKey failed: \(registerStatus)")
            return false
        }
        return true
    }

    func unregister() {
        if let hotKeyRef { UnregisterEventHotKey(hotKeyRef); self.hotKeyRef = nil }
        if let eventHandler { RemoveEventHandler(eventHandler); self.eventHandler = nil }
    }
}
