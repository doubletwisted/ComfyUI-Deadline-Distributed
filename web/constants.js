export const BUTTON_STYLES = {
    // Base styles with unified padding
    base: "width: 100%; padding: 4px 14px; color: white; border: none; border-radius: 4px; cursor: pointer; transition: all 0.2s; font-size: 12px; font-weight: 500;",
    
    // Context-specific combined styles
    workerControl: "flex: 1; font-size: 11px;",
    
    // Layout modifiers
    hidden: "display: none;",
    marginLeftAuto: "margin-left: auto;",
    
    // Color variants
    cancel: "background-color: #555;",
    info: "background-color: #333;",
    success: "background-color: #4a7c4a;",
    error: "background-color: #7c4a4a;",
    launch: "background-color: #4a7c4a;",
    stop: "background-color: #7c4a4a;",
    log: "background-color: #685434;",
    clearMemory: "background-color: #555; padding: 6px 14px;",
    interrupt: "background-color: #555; padding: 6px 14px;",
    claim: "background-color: #4a7c4a; padding: 6px 12px;",
    release: "background-color: #7c4a4a; padding: 6px 12px;",
};

export const STATUS_COLORS = {
    DISABLED_GRAY: "#666",
    OFFLINE_RED: "#c04c4c",
    ONLINE_GREEN: "#3ca03c",
    PROCESSING_YELLOW: "#f0ad4e"
};

export const UI_COLORS = {
    MUTED_TEXT: "#888",
    SECONDARY_TEXT: "#ccc",
    BORDER_LIGHT: "#555",
    BORDER_DARK: "#444",
    BORDER_DARKER: "#3a3a3a",
    BACKGROUND_DARK: "#2a2a2a",
    BACKGROUND_DARKER: "#1e1e1e",
    ICON_COLOR: "#666",
    ACCENT_COLOR: "#777"
};

export const PULSE_ANIMATION_CSS = `
    @keyframes pulse {
        0% {
            opacity: 1;
            transform: scale(0.8);
            box-shadow: 0 0 0 0 rgba(240, 173, 78, 0.7);
        }
        50% {
            opacity: 0.3;
            transform: scale(1.1);
            box-shadow: 0 0 0 6px rgba(240, 173, 78, 0);
        }
        100% {
            opacity: 1;
            transform: scale(0.8);
            box-shadow: 0 0 0 0 rgba(240, 173, 78, 0);
        }
    }
    .status-pulsing {
        animation: pulse 1.2s ease-in-out infinite;
        transform-origin: center;
    }
    
    /* Button hover effects */
    .distributed-button:hover:not(:disabled) {
        filter: brightness(1.2);
        transition: filter 0.2s ease;
    }
    .distributed-button:disabled {
        opacity: 0.6;
        cursor: not-allowed;
    }
    
    /* Settings button animation */
    .settings-btn {
        transition: transform 0.2s ease;
    }
    
    
    /* Expanded settings panel */
    .worker-settings {
        max-height: 0;
        overflow: hidden;
        opacity: 0;
        transition: max-height 0.3s ease, opacity 0.3s ease, padding 0.3s ease, margin 0.3s ease;
    }
    .worker-settings.expanded {
        max-height: 500px;
        opacity: 1;
        padding: 12px 0;
    }
`;

export const UI_STYLES = {
    statusDot: "display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 10px;",
    controlsDiv: "padding: 0 12px 12px 12px; display: flex; gap: 6px;",
    formGroup: "display: flex; flex-direction: column; gap: 5px;",
    formLabel: "font-size: 12px; color: #ccc; font-weight: 500;",
    formInput: "padding: 6px 10px; background: #2a2a2a; border: 1px solid #444; color: white; font-size: 12px; border-radius: 4px; transition: border-color 0.2s;",
    
    // Card styles
    cardBase: "margin-bottom: 12px; border-radius: 6px; overflow: hidden; display: flex;",
    workerCard: "margin-bottom: 12px; border-radius: 6px; overflow: hidden; display: flex; background: #2a2a2a;",
    cardBlueprint: "border: 2px dashed #555; cursor: pointer; transition: all 0.2s ease; background: rgba(255, 255, 255, 0.02);",
    cardAdd: "border: 1px dashed #444; cursor: pointer; transition: all 0.2s ease; background: transparent;",
    
    // Column styles
    columnBase: "display: flex; align-items: center; justify-content: center;",
    checkboxColumn: "flex: 0 0 44px; display: flex; align-items: center; justify-content: center; border-right: 1px solid #3a3a3a; cursor: default; background: rgba(0,0,0,0.1);",
    contentColumn: "flex: 1; display: flex; flex-direction: column; transition: background-color 0.2s ease;",
    iconColumn: "width: 44px; flex-shrink: 0; font-size: 20px; color: #666;",
    
    // Row and content styles
    infoRow: "display: flex; align-items: center; padding: 12px; cursor: pointer; min-height: 64px;",
    workerContent: "display: flex; align-items: center; gap: 10px; flex: 1;",
    
    // Form and controls styles
    buttonGroup: "display: flex; gap: 4px; margin-top: 10px;",
    settingsForm: "display: flex; flex-direction: column; gap: 10px;",
    checkboxGroup: "display: flex; align-items: center; gap: 8px; margin: 5px 0;",
    formLabelClickable: "font-size: 12px; color: #ccc; cursor: pointer;",
    settingsToggle: "display: flex; align-items: center; gap: 6px; padding: 4px 0; cursor: pointer; user-select: none;",
    controlsWrapper: "display: flex; gap: 6px; align-items: stretch; width: 100%;",
    
    // Existing styles
    settingsArrow: "font-size: 12px; color: #888; transition: all 0.2s ease; margin-left: auto; padding: 4px;",
    infoBox: "background-color: #333; color: #999; padding: 5px 14px; border-radius: 4px; font-size: 11px; text-align: center; flex: 1; font-weight: 500;",
    workerSettings: "margin: 0 12px; padding: 0 12px; background: #1e1e1e; border-radius: 4px; border: 1px solid #2a2a2a;",

    // Progress UI
    progressRow: "display: none; align-items: center; gap: 8px; padding: 0 12px 8px 12px;",
    progressBarContainer: "flex: 1; height: 6px; background: #333; border: 1px solid #444; border-radius: 4px; overflow: hidden;",
    progressBarFill: "height: 100%; background: #4a90e2; width: 0%; transition: width 0.2s ease;",
    progressText: "font-size: 11px; color: #aaa; min-width: 90px; text-align: right;"
};

export const TIMEOUTS = {
    DEFAULT_FETCH: 5000, // ms for general API calls
    STATUS_CHECK: 1200, // ms for status checks
    LAUNCH: 90000, // ms for worker launch (longer for model loading)
    RETRY_DELAY: 1000, // initial delay for exponential backoff
    MAX_RETRIES: 3, // max retry attempts
    
    // UI feedback delays
    BUTTON_RESET: 3000, // button text/state reset after actions
    FLASH_SHORT: 1000, // brief success feedback
    FLASH_MEDIUM: 1500, // medium error feedback  
    FLASH_LONG: 2000, // longer error feedback
    
    // Operational delays
    POST_ACTION_DELAY: 500, // delay after operations before status checks
    STATUS_CHECK_DELAY: 100, // brief delay before status checks
    
    // Background tasks
    LOG_REFRESH: 2000, // log auto-refresh interval
    IMAGE_CACHE_CLEAR: 30000 // delay before clearing image cache
};