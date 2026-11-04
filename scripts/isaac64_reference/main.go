package main

import "fmt"

func main() {
	keys := []uint64{0, 1, 12345, 0x1234567890abcdef}
	for _, k := range keys {
		c := CreateISAacInst(k)
		fmt.Printf("KEY %d:", k)
		for i := 0; i < 6; i++ {
			fmt.Printf(" %d", c.ISAacRandom())
		}
		fmt.Println()
	}
}
