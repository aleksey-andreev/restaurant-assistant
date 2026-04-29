# Toka MCP Schema Bundle (Draft 2020-12)

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://example.com/schemas/restaurant-assistant/toka-mcp-bundle.json",
  "title": "Restaurant Assistant Toka MCP Bundle",
  "description": "Bundle of request/response schemas for Toka MCP subagent.",
  "$defs": {
    "RestaurantRef": {
      "$id": "#RestaurantRef",
      "title": "RestaurantRef",
      "description": "Stable restaurant reference passed from main agent to booking subagent.",
      "type": "object",
      "additionalProperties": false,
      "required": ["source", "source_id", "name"],
      "properties": {
        "source": {
          "type": "string",
          "description": "Catalog source of the card.",
          "examples": ["afisha"]
        },
        "source_id": {
          "type": "string",
          "description": "Stable unique id inside source.",
          "examples": ["afisha:place:987654"]
        },
        "name": {
          "type": "string"
        },
        "address": {
          "type": "string"
        },
        "url": {
          "type": "string",
          "format": "uri"
        },
        "geo": {
          "type": "object",
          "additionalProperties": false,
          "required": ["lat", "lon"],
          "properties": {
            "lat": {
              "type": "number"
            },
            "lon": {
              "type": "number"
            }
          }
        },
        "resolver_hint": {
          "type": "object",
          "description": "Optional hints for resolver (city, district, mall).",
          "additionalProperties": true
        },
        "toka_mapping": {
          "type": "object",
          "description": "Resolved mapping to Toka identifiers, if known.",
          "additionalProperties": false,
          "required": ["organization_id", "store_id"],
          "properties": {
            "organization_id": {
              "type": "string"
            },
            "store_id": {
              "type": "string"
            },
            "confidence": {
              "type": "number",
              "minimum": 0,
              "maximum": 1
            },
            "resolved_at": {
              "type": "string",
              "format": "date-time"
            }
          }
        }
      }
    },
    "TokaError": {
      "$id": "#TokaError",
      "type": "object",
      "additionalProperties": false,
      "required": ["code", "message"],
      "properties": {
        "code": {
          "type": "string",
          "enum": [
            "TOKA_API_ERROR",
            "TOKA_UNKNOWN_ERROR",
            "TOKA_TIMEOUT",
            "TOKA_AUTH_FAILED",
            "NO_TABLE_AVAILABLE",
            "TABLE_NOT_FOUND",
            "RESOLVER_NOT_CONFIGURED",
            "RESOLVER_AMBIGUOUS",
            "TOKA_GATEWAY_ERROR"
          ],
          "description": "Machine-readable error code.",
          "examples": [
            "TOKA_TIMEOUT",
            "NO_TABLE_AVAILABLE",
            "TOKA_AUTH_FAILED",
            "RESOLVER_AMBIGUOUS"
          ]
        },
        "message": {
          "type": "string"
        },
        "retriable": {
          "type": "boolean"
        },
        "details": {
          "type": "object",
          "additionalProperties": true
        }
      }
    },
    "TokaToolResultBase": {
      "$id": "#TokaToolResultBase",
      "type": "object",
      "additionalProperties": false,
      "required": ["ok"],
      "properties": {
        "ok": {
          "type": "boolean"
        },
        "data": {},
        "error": {
          "$ref": "#/$defs/TokaError"
        }
      },
      "allOf": [
        {
          "if": {
            "properties": {
              "ok": {
                "const": true
              }
            }
          },
          "then": {
            "required": ["data"]
          }
        },
        {
          "if": {
            "properties": {
              "ok": {
                "const": false
              }
            }
          },
          "then": {
            "required": ["error"]
          }
        }
      ]
    },
    "TokaFindCapacityRequest": {
      "$id": "#TokaFindCapacityRequest",
      "title": "TokaFindCapacityRequest",
      "type": "object",
      "additionalProperties": false,
      "required": ["candidate_ref", "party_size"],
      "properties": {
        "candidate_ref": {
          "$ref": "#/$defs/RestaurantRef"
        },
        "party_size": {
          "type": "integer",
          "minimum": 1
        },
        "starts_at": {
          "type": "string",
          "format": "date-time",
          "description": "Optional slot hint for future time-aware checks."
        }
      }
    },
    "TokaFindCapacityData": {
      "$id": "#TokaFindCapacityData",
      "title": "TokaFindCapacityData",
      "type": "object",
      "additionalProperties": false,
      "required": ["capacity_verified", "party_size"],
      "properties": {
        "capacity_verified": {
          "type": "boolean"
        },
        "party_size": {
          "type": "integer",
          "minimum": 1
        },
        "max_capacity": {
          "type": "integer",
          "minimum": 0
        },
        "message": {
          "type": "string"
        },
        "resolved": {
          "type": "object",
          "additionalProperties": false,
          "required": ["organization_id", "store_id"],
          "properties": {
            "organization_id": {
              "type": "string"
            },
            "store_id": {
              "type": "string"
            }
          }
        }
      }
    },
    "TokaFindCapacityResult": {
      "$id": "#TokaFindCapacityResult",
      "allOf": [
        {
          "$ref": "#/$defs/TokaToolResultBase"
        },
        {
          "if": {
            "properties": {
              "ok": {
                "const": true
              }
            }
          },
          "then": {
            "properties": {
              "data": {
                "$ref": "#/$defs/TokaFindCapacityData"
              }
            }
          }
        }
      ]
    },
    "TokaCreateReservationRequest": {
      "$id": "#TokaCreateReservationRequest",
      "title": "TokaCreateReservationRequest",
      "type": "object",
      "additionalProperties": false,
      "required": [
        "restaurant_ref",
        "starts_at",
        "guest_count",
        "guest_name",
        "guest_phone"
      ],
      "properties": {
        "restaurant_ref": {
          "$ref": "#/$defs/RestaurantRef"
        },
        "starts_at": {
          "type": "string",
          "format": "date-time"
        },
        "duration_minutes": {
          "type": "integer",
          "minimum": 1
        },
        "guest_count": {
          "type": "integer",
          "minimum": 1
        },
        "guest_name": {
          "type": "string",
          "minLength": 1
        },
        "guest_phone": {
          "type": "string",
          "minLength": 3
        },
        "notes": {
          "type": "string"
        },
        "idempotency_key": {
          "type": "string",
          "description": "Optional dedup key for repeated submits."
        }
      }
    },
    "TokaCreateReservationData": {
      "$id": "#TokaCreateReservationData",
      "title": "TokaCreateReservationData",
      "type": "object",
      "additionalProperties": false,
      "required": ["reservation_id", "starts_at"],
      "properties": {
        "reservation_id": {
          "type": "string"
        },
        "starts_at": {
          "type": "string",
          "format": "date-time"
        },
        "guest_count": {
          "type": "integer",
          "minimum": 1
        },
        "guest_name": {
          "type": "string"
        },
        "guest_phone": {
          "type": "string"
        },
        "table_id": {
          "type": "string"
        },
        "restaurant_name": {
          "type": "string"
        },
        "restaurant_address": {
          "type": "string"
        },
        "raw": {
          "type": "object",
          "description": "Optional raw fragment from Toka response for debugging.",
          "additionalProperties": true
        }
      }
    },
    "TokaCreateReservationResult": {
      "$id": "#TokaCreateReservationResult",
      "allOf": [
        {
          "$ref": "#/$defs/TokaToolResultBase"
        },
        {
          "if": {
            "properties": {
              "ok": {
                "const": true
              }
            }
          },
          "then": {
            "properties": {
              "data": {
                "$ref": "#/$defs/TokaCreateReservationData"
              }
            }
          }
        }
      ]
    },
    "TokaListOrganizationsResultData": {
      "$id": "#TokaListOrganizationsResultData",
      "type": "object",
      "additionalProperties": false,
      "required": ["organizations"],
      "properties": {
        "organizations": {
          "type": "array",
          "items": {
            "type": "object",
            "additionalProperties": true
          }
        }
      }
    },
    "TokaListStoresRequest": {
      "$id": "#TokaListStoresRequest",
      "type": "object",
      "additionalProperties": false,
      "required": ["organization_id"],
      "properties": {
        "organization_id": {
          "type": "string"
        }
      }
    },
    "TokaListStoresResultData": {
      "$id": "#TokaListStoresResultData",
      "type": "object",
      "additionalProperties": false,
      "required": ["stores"],
      "properties": {
        "stores": {
          "type": "array",
          "items": {
            "type": "object",
            "additionalProperties": true
          }
        }
      }
    },
    "TokaGetHallsAndTablesRequest": {
      "$id": "#TokaGetHallsAndTablesRequest",
      "type": "object",
      "additionalProperties": false,
      "required": ["organization_id", "store_id"],
      "properties": {
        "organization_id": {
          "type": "string"
        },
        "store_id": {
          "type": "string"
        }
      }
    },
    "TokaGetHallsAndTablesResultData": {
      "$id": "#TokaGetHallsAndTablesResultData",
      "type": "object",
      "additionalProperties": false,
      "required": ["halls"],
      "properties": {
        "halls": {
          "type": "array",
          "items": {
            "type": "object",
            "additionalProperties": true
          }
        }
      }
    }
  },
  "type": "object",
  "additionalProperties": false,
  "required": ["tools"],
  "properties": {
    "tools": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "toka_find_capacity",
        "toka_create_reservation",
        "toka_list_organizations",
        "toka_list_stores",
        "toka_get_halls_and_tables"
      ],
      "properties": {
        "toka_find_capacity": {
          "type": "object",
          "additionalProperties": false,
          "required": ["request", "response"],
          "properties": {
            "request": {
              "$ref": "#/$defs/TokaFindCapacityRequest"
            },
            "response": {
              "$ref": "#/$defs/TokaFindCapacityResult"
            }
          }
        },
        "toka_create_reservation": {
          "type": "object",
          "additionalProperties": false,
          "required": ["request", "response"],
          "properties": {
            "request": {
              "$ref": "#/$defs/TokaCreateReservationRequest"
            },
            "response": {
              "$ref": "#/$defs/TokaCreateReservationResult"
            }
          }
        },
        "toka_list_organizations": {
          "type": "object",
          "additionalProperties": false,
          "required": ["request", "response"],
          "properties": {
            "request": {
              "type": "object",
              "additionalProperties": false,
              "description": "No arguments required for the current endpoint."
            },
            "response": {
              "allOf": [
                {
                  "$ref": "#/$defs/TokaToolResultBase"
                },
                {
                  "if": {
                    "properties": {
                      "ok": {
                        "const": true
                      }
                    }
                  },
                  "then": {
                    "properties": {
                      "data": {
                        "$ref": "#/$defs/TokaListOrganizationsResultData"
                      }
                    }
                  }
                }
              ]
            }
          }
        },
        "toka_list_stores": {
          "type": "object",
          "additionalProperties": false,
          "required": ["request", "response"],
          "properties": {
            "request": {
              "$ref": "#/$defs/TokaListStoresRequest"
            },
            "response": {
              "allOf": [
                {
                  "$ref": "#/$defs/TokaToolResultBase"
                },
                {
                  "if": {
                    "properties": {
                      "ok": {
                        "const": true
                      }
                    }
                  },
                  "then": {
                    "properties": {
                      "data": {
                        "$ref": "#/$defs/TokaListStoresResultData"
                      }
                    }
                  }
                }
              ]
            }
          }
        },
        "toka_get_halls_and_tables": {
          "type": "object",
          "additionalProperties": false,
          "required": ["request", "response"],
          "properties": {
            "request": {
              "$ref": "#/$defs/TokaGetHallsAndTablesRequest"
            },
            "response": {
              "allOf": [
                {
                  "$ref": "#/$defs/TokaToolResultBase"
                },
                {
                  "if": {
                    "properties": {
                      "ok": {
                        "const": true
                      }
                    }
                  },
                  "then": {
                    "properties": {
                      "data": {
                        "$ref": "#/$defs/TokaGetHallsAndTablesResultData"
                      }
                    }
                  }
                }
              ]
            }
          }
        }
      }
    }
  }
}
```

