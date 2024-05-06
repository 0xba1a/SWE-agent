import React from 'react';

import "../static/message.css";
import "../static/agentMessage.css";

const AgentMessage = ({ item, handleMouseEnter, isHighlighted, feedRef }) => {
    const stepClass = item.step !== null ? `step${item.step}` : '';
    const highlightClass = isHighlighted ? 'highlight' : '';

    return (
        <div 
            className={`message ${item.format} ${stepClass} ${highlightClass}`}
            onMouseEnter={() => handleMouseEnter(item, feedRef)}
        >
            { item.title && <span className="agentMessageTitle badge badge-dark">{item.title}</span>}
            <span>{item.message}</span>
        </div>
    );
};

export default AgentMessage;